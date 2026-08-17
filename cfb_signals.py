"""CFB Betting Signal Layer -- deterministic per-game Signal Scores for
college football bet types. v1 scores ONLY moneyline, mirroring nfl_signals.py
(other markets -- spread, totals -- are deliberately not built here).

The generic scoring math (tanh-squashed weighted lean -> threshold -> side)
now comes from signal_core.py, shared with nfl_signals.py. It used to be a
local copy -- this module and nfl_signals.py each carried their own -- and the
extraction was verified equivalent over exhaustive input grids before the
switch (see the migration PR).

What stays HERE is the sport wiring, which is not shared and should not be:
SIGNAL_SPECS, _base_signals, list_markets, score_game and build_inputs.
betting_signals.py (MLB) still carries its own copy of the core and is
migrated separately, deliberately last, because it is the live sport.
implied_total.py is untouched.

Config-driven and sport-keyed like the others: config["betting_signals"]["cfb"]
holds the weights/scales/threshold. EVERY WEIGHT AND SCALE IN CONFIG IS A FLAT,
PRE-CALIBRATION PLACEHOLDER (see config.yaml's cfb block). Nothing here has
been backtested against anything; the calibration pass is the next PR, exactly
as it was for NFL (#34 shipped flat, #35 measured and replaced them). Nothing
in that config should be read as a tuned number yet. The *direction* each
metric favors (higher offensive PPA good, lower PPA allowed good) is intrinsic
to the metric and lives here in code, not in config -- the same split
betting_signals.py and nfl_signals.py both use.

WHAT THIS MODULE DOES NOT HAVE, and why: no availability override. MLB has one
(probable starter scratched) and NFL has one (starting QB Out/Doubtful),
because both data sources publish the input. College football has no public
injury report of any kind -- no conference-wide mandate, and CFBD publishes
none -- so there is nothing to key an override on. score_game therefore takes
no `availability` argument at all, rather than accepting one that could only
ever be empty. See fetchers/cfb.py's module docstring.
"""

import signal_core
from signal_core import (coerce as _coerce, finalize as _finalize,
                         paired as _paired, raw_lean as _raw_lean,
                         round_half_up as _round)


# Each base signal's definition -- which raw build_inputs() keys feed it, which
# config scale key normalizes it, and which direction favors home ("higher"
# raw home-minus-away value leans home; "lower" leans home the other way, e.g.
# a lower PPA allowed is better defense). This is the SINGLE place that
# mapping is defined, so a signal's definition cannot drift between what
# score_game computes live and what a future cfb_backtest.py measures to
# calibrate it -- the same single-source-of-truth arrangement
# nfl_signals.SIGNAL_SPECS has with nfl_backtest.py. Order is display order
# only; it carries no weighting meaning (weights live in config).
#
# Three signals, matching what CFBD's bulk endpoints actually publish per
# team-game. Scoring margin is deliberately NOT here: NFL's calibration
# measured it as strongly collinear with offensive EPA and dropped it (PR
# #35), so it ships unweighted in fetchers/cfb.py's `context` instead, where
# the CFB backtest can measure it without it influencing a live pick first.
SIGNAL_SPECS = {
    "off_ppa": {"home_key": "home_off_ppa", "away_key": "away_off_ppa",
                "scale_key": "off_ppa_gap", "favors": "higher"},
    "def_ppa_allowed": {"home_key": "home_def_ppa_allowed", "away_key": "away_def_ppa_allowed",
                        "scale_key": "def_ppa_gap", "favors": "lower"},
    "turnover_diff": {"home_key": "home_turnover_diff", "away_key": "away_turnover_diff",
                      "scale_key": "turnover_gap", "favors": "higher"},
}


def _base_signals(inp, scales):
    """Every base signal's directional value toward HOME, or None where
    inputs are missing. Iterates SIGNAL_SPECS rather than hardcoding each
    extraction -- see that table's docstring for why."""
    return {name: _paired(inp.get(spec["home_key"]), inp.get(spec["away_key"]),
                          scales[spec["scale_key"]], spec["favors"])
            for name, spec in SIGNAL_SPECS.items()}


def score_game(config, sport_key, inputs):
    """Score every configured bet type for one game (v1: moneyline only).
    Returns {bet_type: {side, score, flags}}. Empty dict if the sport isn't
    configured. `inputs` come from build_inputs.

    Takes no `availability` argument -- see the module docstring."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    bet_types = cfg.get("bet_types") or {}
    if not bet_types:
        return {}
    scales = cfg["scales"]
    min_t = cfg.get("min_threshold", 15)

    sig = _base_signals(inputs, scales)
    home, away = inputs.get("home_abbr"), inputs.get("away_abbr")
    out = {}
    for bt, weights in bet_types.items():
        bt_sig = {k: sig.get(k) for k in weights}
        L, n, agree = _raw_lean(bt_sig, weights)
        out[bt] = _finalize(L, n, agree, min_t, (home, away))
    return out


# Precedence for tie-breaking the standout pick when two markets share the top
# score. v1 has exactly one market, so this only matters once a second CFB bet
# type is added.
_MARKET_PRECEDENCE = ("moneyline",)


def list_markets(scored):
    """Every market carrying a real lean (side != 'No clear lean'), as
    [{bet_type, side, score, flags}] sorted by Signal Score desc."""
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
    See nfl_signals.top_market and signal_core.top_market for why the shared
    helper takes a ranked list rather than the raw `scored` dict."""
    return signal_core.top_market(list_markets(scored), threshold)


def build_inputs(away_abbr, home_abbr, away_off_ppa, home_off_ppa,
                 away_def_ppa_allowed, home_def_ppa_allowed,
                 away_turnover_diff, home_turnover_diff):
    """Assemble the deterministic input dict from fetchers.cfb's already-
    computed team-form values, mirroring nfl_signals.build_inputs' role."""
    return {
        "away_abbr": away_abbr, "home_abbr": home_abbr,
        "away_off_ppa": _coerce(away_off_ppa), "home_off_ppa": _coerce(home_off_ppa),
        "away_def_ppa_allowed": _coerce(away_def_ppa_allowed),
        "home_def_ppa_allowed": _coerce(home_def_ppa_allowed),
        "away_turnover_diff": _coerce(away_turnover_diff),
        "home_turnover_diff": _coerce(home_turnover_diff),
    }
