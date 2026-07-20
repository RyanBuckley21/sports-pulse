"""Betting Signal Layer -- deterministic per-game Signal Scores for MLB bet types.

Scores each configured bet type as a 0-100 conviction toward a named side, from
the already-computed Game entity inputs (home/away 14d OPS, 7d bullpen ERA,
probable-starter season ERA, season series). The AI only EXPLAINS these numbers;
it never invents them -- the same rule as the Pulse Score.

Everything here is deterministic and offline (no network, no model): it runs in
CI alongside the rest of the Game builder, while the AI explanation stays behind
the existing subscription-auth rails.

Config-driven and sport-keyed: `config["betting_signals"][sport_key]` holds the
weights, scales, and thresholds. `mlb` is populated; other sports are reserved
empty. The *direction* each metric favors (higher OPS good, lower ERA good) is
intrinsic to the metric and lives here in code, not in config.

Availability (a probable starter on the IL) is a HARD OVERRIDE applied after the
base calculation -- not a graded signal -- because a scratched starter doesn't
"lean" a market, it materially changes it (see docs/mlb-availability-field-map.md
and the design proposal). It invalidates that side's now-stale probable-ERA
signal, penalizes the team on the side markets, pushes the game total toward
Over, and clamps the first-five / NRFI markets (a bullpen/opener game is
genuinely unpredictable), flagging every market it touches.
"""

import math

# Side-market bet types score toward (+) HOME; totals toward (+) OVER; NRFI/YRFI
# toward (+) YRFI. These label pairs turn the sign of the net lean into a side.
_SIDE_MARKETS = ("moneyline", "run_line", "first_five_moneyline")
_TOTAL_MARKETS = ("game_total", "first_five_total")


def _coerce(v):
    """Parse a numeric input (float, int, or display string like ".812"/"4.02")
    to float, or None if absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round(x):
    """Round half UP (not banker's rounding) so scores match the hand-mocked
    proposal values, e.g. 60.5 -> 61."""
    return int(math.floor(x + 0.5))


def _paired(val_home, val_away, scale, favors):
    """Directional value toward HOME (+1) from a home-vs-away gap, tanh-squashed
    by `scale`. favors='higher' -> a higher home value leans home; favors='lower'
    -> a lower home value leans home. None if either side is missing."""
    if val_home is None or val_away is None:
        return None
    d = math.tanh((val_home - val_away) / scale)
    return d if favors == "higher" else -d


def _total(combined, spec):
    """Directional value toward OVER (+1): how far a combined magnitude sits
    above/below a league baseline, tanh-squashed. None if the combined is
    missing."""
    if combined is None:
        return None
    return math.tanh((combined - spec["base"]) / spec["scale"])


def _solo(val, spec):
    """Directional value toward OVER (+1) from a single value vs a baseline
    (used by Team Total: own offense, opponent bullpen/starter). None if
    missing."""
    if val is None:
        return None
    return math.tanh((val - spec["base"]) / spec["scale"])


def _add(a, b):
    return None if (a is None or b is None) else a + b


def _base_signals(inp, scales):
    """Every base signal's directional value (toward HOME for side markets, OVER
    for totals), or None where inputs are missing. Availability is NOT applied
    here -- it's a later override."""
    ho, ao = inp.get("home_ops"), inp.get("away_ops")
    hb, ab = inp.get("home_bullpen"), inp.get("away_bullpen")
    hs, as_ = inp.get("home_starter_era"), inp.get("away_starter_era")
    hw, aw = inp.get("series_home_wins"), inp.get("series_away_wins")
    series = None
    if hw is not None and aw is not None and (hw + aw) > 0:
        series = (hw - aw) / float(hw + aw)
    return {
        "team_ops": _paired(ho, ao, scales["ops_gap"], "higher"),
        "bullpen_era": _paired(hb, ab, scales["bullpen_gap"], "lower"),
        "probable_era": _paired(hs, as_, scales["era_gap"], "lower"),
        "season_series": series,
        "combined_ops": _total(_add(ao, ho), scales["ops_total"]),
        "combined_starter_era": _total(_add(as_, hs), scales["starter_total"]),
        "combined_bullpen_era": _total(_add(ab, hb), scales["bullpen_total"]),
    }


def _raw_lean(sig, weights):
    """Weighted net lean L in [-1, 1] over the available (non-None) signals,
    renormalized by their weights. A signal present with value 0 (e.g. an even
    series) still counts toward the weight sum -- it's a real neutral input, not
    a missing one. Returns (L, n_available, n_agreeing) or (None, 0, 0)."""
    pairs = [(sig.get(k), w) for k, w in weights.items() if sig.get(k) is not None]
    if not pairs:
        return None, 0, 0
    wsum = sum(w for _, w in pairs)
    if wsum <= 0:
        return None, 0, 0
    L = sum(d * w for d, w in pairs) / wsum
    agree = sum(1 for d, _ in pairs if abs(d) > 1e-9 and (d > 0) == (L > 0))
    return L, len(pairs), agree


def _finalize(L, n_avail, n_agree, threshold, labels, flags=(), force_aligned=False):
    """Turn a net lean into {side, score, flags}. 'No clear lean' when the score
    is under threshold, or (for multi-signal bets) fewer than 2 signals agree
    with the net direction. `force_aligned` bypasses the alignment guard when an
    exogenous availability penalty has been applied."""
    flags = sorted(set(flags))
    if L is None:
        return {"side": "No clear lean", "score": 0, "flags": flags}
    score = _round(100 * min(1.0, abs(L)))
    aligned = True if (force_aligned or n_avail < 2) else (n_agree >= 2)
    if score >= threshold and aligned:
        side = labels[0] if L >= 0 else labels[1]
    else:
        side = "No clear lean"
    return {"side": side, "score": score, "flags": flags}


def _labels_for(bet_type, home_abbr, away_abbr):
    if bet_type in _SIDE_MARKETS:
        return (home_abbr, away_abbr)
    if bet_type == "nrfi_yrfi":
        return ("YRFI", "NRFI")
    return ("Over", "Under")  # totals


def _team_total(t_ops, opp_bullpen, opp_starter, weights, scales, threshold, abbr):
    """Per-team Over/Under lean: the team's own offense vs baseline plus the
    opponent's bullpen/starter weakness. Emitted for BOTH teams -- Team Total is
    two separately-placeable bets, so we never collapse to one side."""
    sig = {
        "team_ops": _solo(t_ops, scales["ops_solo"]),
        "opp_bullpen_era": _solo(opp_bullpen, scales["bullpen_solo"]),
        "opp_starter_era": _solo(opp_starter, scales["era_solo"]),
    }
    L, n, agree = _raw_lean(sig, weights)
    out = _finalize(L, n, agree, threshold, ("Over", "Under"))
    out["abbr"] = abbr
    return out


def _availability_flags(availability):
    return [k for k in ("away_probable_out", "home_probable_out") if availability.get(k)]


def score_game(config, sport_key, inputs, availability=None):
    """Score every configured bet type for one game. Returns
    {bet_type: {side, score, flags}} (+ team_total: {away, home}). Empty dict if
    the sport isn't configured. `inputs` are the deterministic game values;
    `availability` (optional) is {away_probable_out: bool, home_probable_out:
    bool}."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    bet_types = cfg.get("bet_types") or {}
    if not bet_types:
        return {}
    scales = cfg["scales"]
    min_t = cfg.get("min_threshold", 15)
    rl_t = cfg.get("run_line_threshold", min_t)
    availability = availability or {}
    any_out = bool(_availability_flags(availability))

    sig = _base_signals(inputs, scales)
    home, away = inputs.get("home_abbr"), inputs.get("away_abbr")
    out = {}

    for bt, weights in bet_types.items():
        if bt == "team_total":
            continue  # handled separately (per side)
        labels = _labels_for(bt, home, away)
        threshold = rl_t if bt == "run_line" else min_t

        # Availability: clamp the first-five / NRFI markets -- an opener/bullpen
        # game with no announced replacement is genuinely unpredictable there.
        if any_out and bt in ("first_five_moneyline", "first_five_total", "nrfi_yrfi"):
            out[bt] = {"side": "No clear lean", "score": 0,
                       "flags": sorted(set(_availability_flags(availability) + ["starter_scratched"]))}
            continue

        w = dict(weights)
        bt_sig = {k: sig.get(k) for k in w}
        # Availability: the scratched side's probable-ERA reading is now stale --
        # drop it before computing the base lean for the side markets.
        if any_out and bt in ("moneyline", "run_line"):
            w.pop("probable_era", None)
            bt_sig.pop("probable_era", None)

        L, n, agree = _raw_lean(bt_sig, w)

        if any_out and bt in ("moneyline", "run_line"):
            # Penalize the team that lost its starter, toward the opponent.
            base = 0.0 if L is None else L
            if availability.get("home_probable_out"):
                base -= 0.30
            if availability.get("away_probable_out"):
                base += 0.30
            out[bt] = _finalize(base, n, agree, threshold, labels,
                                flags=_availability_flags(availability), force_aligned=True)
        else:
            out[bt] = _finalize(L, n, agree, threshold, labels)

    # Game total: an opener/bullpen game leans Over -- push after the base calc.
    if any_out and "game_total" in out:
        base = _lean_of(out["game_total"], ("Over", "Under")) + 0.20
        out["game_total"] = _finalize(base, 2, 2, min_t, ("Over", "Under"),
                                      flags=out["game_total"]["flags"] + _availability_flags(availability),
                                      force_aligned=True)

    # Team Total -- both sides.
    if "team_total" in bet_types:
        w = bet_types["team_total"]
        out["team_total"] = {
            "away": _team_total(inputs.get("away_ops"), inputs.get("home_bullpen"),
                                inputs.get("home_starter_era"), w, scales, min_t, away),
            "home": _team_total(inputs.get("home_ops"), inputs.get("away_bullpen"),
                                inputs.get("away_starter_era"), w, scales, min_t, home),
        }

    return out


def _lean_of(entry, labels):
    """Reconstruct the signed lean (~L) from a finalized {side, score} entry.
    'No clear lean' collapses to 0 (its sign was already sub-threshold)."""
    if entry["side"] == labels[0]:
        return entry["score"] / 100.0
    if entry["side"] == labels[1]:
        return -entry["score"] / 100.0
    return 0.0


def build_inputs(away_ref, home_ref, away_ops, home_ops, away_bullpen, home_bullpen,
                 away_era, home_era, series):
    """Assemble the deterministic input dict from the Game builder's already-
    computed values. `series` is the (away_wins, away_losses) tuple from
    season_series (away perspective), or None."""
    aw = al = None
    if series is not None:
        aw, al = series  # away wins, away losses (= home wins)
    return {
        "away_abbr": away_ref.get("abbr"), "home_abbr": home_ref.get("abbr"),
        "away_ops": _coerce(away_ops), "home_ops": _coerce(home_ops),
        "away_bullpen": _coerce(away_bullpen), "home_bullpen": _coerce(home_bullpen),
        "away_starter_era": _coerce(away_era), "home_starter_era": _coerce(home_era),
        "series_away_wins": aw, "series_home_wins": al,
    }
