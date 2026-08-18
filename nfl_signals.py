"""NFL Betting Signal Layer -- deterministic per-game Signal Scores for NFL
bet types. v1 scores ONLY moneyline (see the design review); other markets
(spread, totals) are deliberately not built here, same as CFB is deliberately
not this pass.

The generic scoring math (tanh-squashed weighted lean -> threshold -> side)
now comes from signal_core.py, shared with cfb_signals.py. It used to be a
local copy; the extraction was verified equivalent over exhaustive input
grids before the switch (see the migration PR).

What stays HERE is the sport wiring: SIGNAL_SPECS, _base_signals,
list_markets, score_game, build_inputs and the QB-availability override.
betting_signals.py (MLB) still carries its own copy of the core and is
migrated separately, deliberately last, because it is the live sport.
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

import signal_core
from signal_core import (coerce as _coerce, finalize as _finalize,
                         paired as _paired, raw_lean as _raw_lean,
                         round_half_up as _round)

_SIDE_MARKETS = ("moneyline",)


# Each base signal's definition -- which raw build_inputs() keys feed it,
# which config scale key normalizes it, and which direction favors home
# ("higher" raw home-minus-away value leans home; "lower" leans home the
# other way, e.g. a lower EPA allowed is better defense). This is the SINGLE
# place that mapping is defined: _base_signals (below) reads it to build the
# live, config-scaled, tanh-squashed lean, and nfl_backtest.py's reliability
# measurement reads the SAME table to build the raw, unscaled gap it
# correlates against real outcomes -- so a signal's definition cannot drift
# between what score_game actually computes and what the backtest measured
# to calibrate it. Order here is display/iteration order only; it carries no
# weighting meaning (weights live in config).
SIGNAL_SPECS = {
    "off_epa": {"home_key": "home_off_epa", "away_key": "away_off_epa",
                "scale_key": "off_epa_gap", "favors": "higher"},
    "def_epa_allowed": {"home_key": "home_def_epa_allowed", "away_key": "away_def_epa_allowed",
                        "scale_key": "def_epa_gap", "favors": "lower"},
    "turnover_diff": {"home_key": "home_turnover_diff", "away_key": "away_turnover_diff",
                      "scale_key": "turnover_gap", "favors": "higher"},
    "scoring_margin": {"home_key": "home_scoring_margin", "away_key": "away_scoring_margin",
                       "scale_key": "margin_gap", "favors": "higher"},
    "rest_diff": {"home_key": "home_rest", "away_key": "away_rest",
                 "scale_key": "rest_gap", "favors": "higher"},
}


def _base_signals(inp, scales):
    """Every base signal's directional value toward HOME, or None where
    inputs are missing. Availability is NOT applied here -- it's the later
    override (see score_game). Iterates SIGNAL_SPECS rather than hardcoding
    each signal's extraction -- see that table's docstring for why."""
    return {name: _paired(inp.get(spec["home_key"]), inp.get(spec["away_key"]),
                          scales[spec["scale_key"]], spec["favors"])
            for name, spec in SIGNAL_SPECS.items()}


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
    """Delegates to signal_core.top_market, passing THIS module's ranking in.

    The shared helper takes an already-ranked list rather than the raw
    `scored` dict on purpose: list_markets differs per sport (MLB expands
    team_total into per-side candidates; NFL does not), and a shared function
    that called list_markets itself would bind to the wrong sport's rules.
    See signal_core.top_market."""
    return signal_core.top_market(list_markets(scored), threshold)


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
