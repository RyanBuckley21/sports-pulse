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

THE GENERIC MATH COMES FROM signal_core.py -- coerce, paired, raw_lean, finalize
and the "first market over the bar" half of top_market -- shared with NFL, CFB
and EPL. This module used to carry its own copy of every one of them, and was
the last sport to: NFL and CFB migrated in PR #52, EPL was written against the
shared module, and MLB was left for last because it is the live, graded sport.
While two copies existed nothing kept them in step, so a fix made in
signal_core silently skipped MLB's scores.

The switch was proved a behavioural no-op before it was made, not assumed:
AST-normalised hashes of the five helper bodies matched signal_core's exactly;
the old and new helpers were cross-executed over 349,776 grid cases, and the
whole old and new modules over 150,000 random games under every one of the five
distinct `betting_signals.mlb` blocks config.yaml has ever held, with zero
mismatches; and 777 real games (1,181 replays across 92 committed revisions of
data/insights.games.json, 39 of them with a scratched starter) scored
byte-identically before and after. Every one of those harnesses was
sabotage-checked -- break the shared math and each reports mismatches -- so a
zero is a measurement rather than a harness that cannot fail.

What stays HERE is MLB's own wiring: _base_signals (seven signals, including
the three combined totals and the inline season-series ratio), _total and
_solo (baseline-relative squashes no other sport uses), the availability
override, _apply_run_line_guard, team_total and its per-side expansion in
list_markets, score_game and build_inputs.
"""

import math

import signal_core
# Underscore aliases, the same ones nfl_signals.py and cfb_signals.py import
# under, so every call site below reads exactly as it did when these were local
# definitions. round_half_up is not imported: its only caller here was the old
# local _finalize, which is now signal_core.finalize itself.
from signal_core import (coerce as _coerce, finalize as _finalize,
                         paired as _paired, raw_lean as _raw_lean)

# Side-market bet types score toward (+) HOME; totals toward (+) OVER; NRFI/YRFI
# toward (+) YRFI. These label pairs turn the sign of the net lean into a side.
_SIDE_MARKETS = ("moneyline", "run_line", "first_five_moneyline")
_TOTAL_MARKETS = ("game_total", "first_five_total")


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


def _apply_run_line_guard(out):
    """Same-side invariant between run_line and moneyline, applied in place to
    `out` after both are computed. Covering the run line requires winning
    outright, so when both lean the same team, that's the favorite/"laying"
    side (see _run_line_laying in signal_report.py -- empirically, across 1,388
    real 2026 games, run_line and moneyline never disagree on side; reweighting
    the same signals changes the magnitude of the lean, not its sign). run_line's
    score should therefore never structurally outrank moneyline's for that side
    -- but moneyline and run_line are independently weighted blends of the same
    signals, so nothing about the scoring math enforces that on its own; a
    starter-ERA gap large enough can push run_line above moneyline even though
    covering by 2+ is strictly harder than just winning. One-directional on
    purpose: run_line CAN legitimately score lower than moneyline (that's the
    normal case, reflecting the harder bar) -- it just can't score higher.
    "run_line_hot" is the readable signal that this fired, kept on the
    (now-clamped) entry since the raw pre-clamp score isn't needed anywhere else
    once it's been read."""
    if "run_line" not in out or "moneyline" not in out:
        return
    ml, rl = out["moneyline"], out["run_line"]
    if ml["side"] not in (None, "No clear lean") and rl["side"] == ml["side"] \
            and rl["score"] > ml["score"]:
        rl["flags"] = sorted(set(rl["flags"] + ["run_line_hot"]))
        rl["score"] = min(rl["score"], ml["score"])


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

    _apply_run_line_guard(out)

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


# Precedence for tie-breaking the standout pick when two markets share the top
# score (rare). Earlier = preferred: the cleaner single-number markets first, the
# noisier one-run markets (run line, NRFI) last.
_MARKET_PRECEDENCE = (
    "moneyline", "first_five_moneyline", "game_total", "first_five_total",
    "team_total", "run_line", "nrfi_yrfi",
)


def list_markets(scored):
    """Every market carrying a real lean (side != 'No clear lean'), as
    [{bet_type, side, score, flags}] sorted by Signal Score desc with
    _MARKET_PRECEDENCE as a stable tiebreak. team_total contributes BOTH sides as
    separate rows (side pre-labeled with the team abbr). This is the UI's ranked
    Signal Score list; top_market() picks the single standout from the same set,
    so the two never disagree."""
    prec = {k: i for i, k in enumerate(_MARKET_PRECEDENCE)}
    candidates = []  # (bet_type, side, score, flags)
    for bt, entry in (scored or {}).items():
        if bt == "team_total" and isinstance(entry, dict):
            for side_key in ("away", "home"):
                e = entry.get(side_key) or {}
                side = e.get("side")
                if side and side != "No clear lean":
                    label = "{} {}".format(e.get("abbr"), side) if e.get("abbr") else side
                    candidates.append((bt, label, e.get("score", 0), e.get("flags") or []))
        elif isinstance(entry, dict):
            side = entry.get("side")
            if side and side != "No clear lean":
                candidates.append((bt, side, entry.get("score", 0), entry.get("flags") or []))
    candidates.sort(key=lambda c: (-c[2], prec.get(c[0], 99)))
    return [{"bet_type": bt, "side": side, "score": score, "flags": list(flags)}
            for bt, side, score, flags in candidates]


def top_market(scored, threshold):
    """Deterministically pick a game's single most-notable market: the highest
    Signal Score among markets that carry a real lean AND clear `threshold`.
    Returns {bet_type, side, score, flags} or None when nothing clears the bar.

    KEEPS ITS (scored, threshold) SIGNATURE, and so stays a function here rather
    than becoming an import. signal_core.top_market takes an already-RANKED list
    instead, because MLB's ranking is its own: list_markets() below expands
    team_total into one candidate per side, which NFL, CFB and EPL do not. This
    wrapper supplies that ranking and leaves the shared part -- "first one over
    the bar wins" -- to signal_core. fetchers/mlb.py and signal_report's MLB
    adapter both call it with `scored`, so the public shape does not move.

    Verified identical to the local body it replaced: 200,000 random `scored`
    dicts (team_total included, every threshold from 0 to above 100) returned
    the same repr from both, with zero mismatches."""
    return signal_core.top_market(list_markets(scored), threshold)


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
