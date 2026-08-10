"""NFL Betting Signal Layer -- deterministic per-game Signal Scores for NFL
bet types. v1 scores ONLY moneyline (see the design review); other markets
(spread, totals) are deliberately not built here, same as CFB is deliberately
not this pass.

Mirrors betting_signals.py's math (tanh-squashed weighted lean -> threshold ->
side), DUPLICATED rather than shared: betting_signals.py's generic scoring
helpers are interleaved with MLB-specific bet-type wiring (_SIDE_MARKETS,
run_line, nrfi_yrfi, the probable-pitcher override) throughout a single flat
module, so sharing them would mean editing that already-tuned,
calibration-history-laden file, which the precursor PR's design review
deliberately left untouched. This is NFL's own copy of the same ~80 lines of
math, with NFL's own signal names and its own single-market bet_types set.
betting_signals.py and implied_total.py are untouched by this module.

Config-driven and sport-keyed like betting_signals: config["betting_signals"]["nfl"]
holds the weights/scales/threshold. EVERY WEIGHT AND SCALE IN CONFIG IS A
FLAT, PRE-CALIBRATION PLACEHOLDER (see config.yaml's nfl block) -- unlike
MLB's, which were measured against real production outcomes over months (see
betting_signals.py's own config comments for that history), NFL's have not
been backtested against anything yet. That backtest is the next step, not
this PR; nothing here should be read as a tuned number. The *direction* each
metric favors (higher offensive EPA good, lower EPA allowed good) is
intrinsic to the metric and lives here in code, not in config -- same split
betting_signals.py uses.

Availability (this game's starting QB reported Out/Doubtful) is a HARD
OVERRIDE applied after the base calculation, not a graded signal -- mirroring
betting_signals.py's probable-starter-out override exactly: a QB change
doesn't "lean" the moneyline, it materially changes it. See
fetchers/nfl.py's get_starting_qb/qb_out for how availability is determined
(a real, flagged limitation: nflverse has no pregame "probable QB" feed, so
this is "whoever started last week", checked against the current injury
report -- not a fabricated signal, but an approximation worth knowing about).
"""

import math

_SIDE_MARKETS = ("moneyline",)


def _coerce(v):
    """Parse a numeric input to float, or None if absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round(x):
    """Round half UP (not banker's rounding), same convention
    betting_signals._round uses."""
    return int(math.floor(x + 0.5))


def _paired(val_home, val_away, scale, favors):
    """Directional value toward HOME (+1) from a home-vs-away gap, tanh-
    squashed by `scale`. favors='higher' -> a higher home value leans home;
    favors='lower' -> a lower home value leans home. None if either side is
    missing."""
    if val_home is None or val_away is None:
        return None
    d = math.tanh((val_home - val_away) / scale)
    return d if favors == "higher" else -d


def _raw_lean(sig, weights):
    """Weighted net lean L in [-1, 1] over the available (non-None) signals,
    renormalized by their weights. A signal present with value 0 still
    counts toward the weight sum -- a real neutral input, not a missing one.
    Returns (L, n_available, n_agreeing) or (None, 0, 0)."""
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
    """Turn a net lean into {side, score, flags}. 'No clear lean' when the
    score is under threshold, or (for multi-signal bets) fewer than 2
    signals agree with the net direction. `force_aligned` bypasses the
    alignment guard when an exogenous availability penalty has been
    applied."""
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


def _base_signals(inp, scales):
    """Every base signal's directional value toward HOME, or None where
    inputs are missing. Availability is NOT applied here -- it's the later
    override (see score_game)."""
    ho, ao = inp.get("home_off_epa"), inp.get("away_off_epa")
    hd, ad = inp.get("home_def_epa_allowed"), inp.get("away_def_epa_allowed")
    ht, at = inp.get("home_turnover_diff"), inp.get("away_turnover_diff")
    hm, am = inp.get("home_scoring_margin"), inp.get("away_scoring_margin")
    hr, ar = inp.get("home_rest"), inp.get("away_rest")
    return {
        "off_epa": _paired(ho, ao, scales["off_epa_gap"], "higher"),
        "def_epa_allowed": _paired(hd, ad, scales["def_epa_gap"], "lower"),
        "turnover_diff": _paired(ht, at, scales["turnover_gap"], "higher"),
        "scoring_margin": _paired(hm, am, scales["margin_gap"], "higher"),
        "rest_diff": _paired(hr, ar, scales["rest_gap"], "higher"),
    }


def _qb_flags(availability):
    return [k for k in ("away_qb_out", "home_qb_out") if availability.get(k)]


def score_game(config, sport_key, inputs, availability=None):
    """Score every configured bet type for one game (v1: moneyline only).
    Returns {bet_type: {side, score, flags}}. Empty dict if the sport isn't
    configured. `inputs` come from build_inputs; `availability` (optional)
    is {away_qb_out: bool, home_qb_out: bool}."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    bet_types = cfg.get("bet_types") or {}
    if not bet_types:
        return {}
    scales = cfg["scales"]
    min_t = cfg.get("min_threshold", 15)
    availability = availability or {}
    any_out = bool(_qb_flags(availability))

    sig = _base_signals(inputs, scales)
    home, away = inputs.get("home_abbr"), inputs.get("away_abbr")
    out = {}

    for bt, weights in bet_types.items():
        labels = (home, away)
        w = dict(weights)
        bt_sig = {k: sig.get(k) for k in w}

        # Availability: the QB-out side's offensive-EPA reading is now stale
        # (it describes a different quarterback's play) -- drop it before
        # computing the base lean, same rule betting_signals applies to a
        # scratched probable starter's ERA.
        if any_out and bt in _SIDE_MARKETS:
            w.pop("off_epa", None)
            bt_sig.pop("off_epa", None)

        L, n, agree = _raw_lean(bt_sig, w)

        if any_out and bt in _SIDE_MARKETS:
            # Penalize the team that lost its starting QB, toward the
            # opponent -- same fixed-penalty shape betting_signals applies
            # to a scratched probable starter.
            base = 0.0 if L is None else L
            if availability.get("home_qb_out"):
                base -= 0.30
            if availability.get("away_qb_out"):
                base += 0.30
            out[bt] = _finalize(base, n, agree, min_t, labels,
                                flags=_qb_flags(availability), force_aligned=True)
        else:
            out[bt] = _finalize(L, n, agree, min_t, labels)

    return out


# Precedence for tie-breaking the standout pick when two markets share the
# top score. v1 has exactly one market, so this only matters once a second
# NFL bet type is added.
_MARKET_PRECEDENCE = ("moneyline",)


def list_markets(scored):
    """Every market carrying a real lean (side != 'No clear lean'), as
    [{bet_type, side, score, flags}] sorted by Signal Score desc. Mirrors
    betting_signals.list_markets, minus the team_total per-side expansion --
    NFL v1 has no team_total market."""
    prec = {k: i for i, k in enumerate(_MARKET_PRECEDENCE)}
    candidates = []
    for bt, entry in (scored or {}).items():
        if not isinstance(entry, dict):
            continue
        side = entry.get("side")
        if side and side != "No clear lean":
            candidates.append((bt, side, entry.get("score", 0), entry.get("flags") or []))
    candidates.sort(key=lambda c: (-c[2], prec.get(c[0], 99)))
    return [{"bet_type": bt, "side": side, "score": score, "flags": list(flags)}
            for bt, side, score, flags in candidates]


def top_market(scored, threshold):
    """Deterministically pick a game's single most-notable market: the
    highest Signal Score among markets that carry a real lean AND clear
    `threshold`. Returns {bet_type, side, score, flags} or None."""
    for m in list_markets(scored):
        if m["score"] >= threshold:
            return dict(m)
    return None


def build_inputs(away_abbr, home_abbr, away_off_epa, home_off_epa,
                 away_def_epa_allowed, home_def_epa_allowed,
                 away_turnover_diff, home_turnover_diff,
                 away_scoring_margin, home_scoring_margin,
                 away_rest, home_rest):
    """Assemble the deterministic input dict from fetchers.nfl's already-
    computed team-form values, mirroring betting_signals.build_inputs' role
    for MLB."""
    return {
        "away_abbr": away_abbr, "home_abbr": home_abbr,
        "away_off_epa": _coerce(away_off_epa), "home_off_epa": _coerce(home_off_epa),
        "away_def_epa_allowed": _coerce(away_def_epa_allowed), "home_def_epa_allowed": _coerce(home_def_epa_allowed),
        "away_turnover_diff": _coerce(away_turnover_diff), "home_turnover_diff": _coerce(home_turnover_diff),
        "away_scoring_margin": _coerce(away_scoring_margin), "home_scoring_margin": _coerce(home_scoring_margin),
        "away_rest": _coerce(away_rest), "home_rest": _coerce(home_rest),
    }
