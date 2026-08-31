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
    # FALLBACK TIERS -- see _FALLBACK_TIERS. Never scored alongside the
    # weighted signals above, or alongside each other.
    #
    # `season_margin` is the SAME QUANTITY as `scoring_margin` above, computed
    # over the same games, and the duplication is deliberate rather than an
    # oversight. scoring_margin is unfloored, carried in `context` for the
    # backtest to measure and excluded from the lean for collinearity with
    # off_epa. This one is floored at five games (fetchers/nfl.
    # SEASON_MARGIN_MIN_GAMES) because a fallback has nothing else beside it to
    # steady a three-game average. Two purposes, two numbers; folding them into
    # one would silently change what the backtest is measuring.
    "season_margin": {"home_key": "home_season_margin", "away_key": "away_season_margin",
                      "scale_key": "season_margin_gap", "favors": "higher"},
    "prior_margin": {"home_key": "home_prior_margin", "away_key": "away_prior_margin",
                     "scale_key": "prior_margin_gap", "favors": "higher"},
}

# WHAT TO SCORE ON WHEN THE CALIBRATED SIGNALS ARE NOT THERE.
#
# Most specific tier first; score_game takes the FIRST tier with any signal
# available and blanks every later one. Nothing here is ever mixed into a
# calibrated lean.
#
# The problem: week 1 has no in-season EPA at all, so every opening-weekend
# game scored 0 / "No clear lean" -- and nflverse publishes a season's
# stats_team release only once that season has games, so the whole of week 1
# is a guaranteed blank, every year. Both tiers below are points margin per
# game off the plain schedule (nfldata's games.csv), which is one keyless
# fetch this fetcher already makes.
#
# WHY THESE TWO, IN THIS ORDER, measured walk-forward over 2002-2025 (6,223
# regular-season games), straight-up win rate at NFL's threshold of 50:
#
#             week 1   weeks 2-3   weeks 4-5   weeks 6-9   weeks 10+
#   prior      66.2%     64.4%       65.2%       63.6%      61.2%
#   season       --        --        61.6%       68.5%      72.6%
#
# so last season leads through week 5 and this season from week 6. Prior-season
# margin's reliability against the eventual points margin is r=+0.27 in week 1
# (95% CI [+0.17, +0.36], 2,000 resamples, stdlib, fixed seed) -- real, and
# well below the same measurement for college football (+0.46), which is what a
# far more compressed league should look like.
#
# THE HANDOFF IS NOT A WEEK NUMBER. season_margin needs five games and so first
# appears in week 6 by construction, which is where the crossover is. Five was
# chosen by measurement, not copied from CFB's three: at a floor of 3 the
# handoff lands in week 4 and the weeks 1-5 hit rate is 63.7%; at 5 it is
# 65.1%, with weeks 6+ unchanged at 71.4%. CFB's own crossover really is at
# week 4, so the two sports differ here on evidence rather than by accident.
_FALLBACK_TIERS = (("season_margin",), ("prior_margin",))
_FALLBACK_SIGNALS = tuple(k for tier in _FALLBACK_TIERS for k in tier)


def _apply_fallback_tiers(sig, weights):
    """`sig` with every tier below the best AVAILABLE one blanked out.

    TIER 0 IS "THE SIGNALS THIS BET TYPE ACTUALLY WEIGHTS", not "every declared
    spec", and that distinction is a bug this had before it was measured. NFL
    declares two specs that carry NO weight: `scoring_margin`, excluded for
    collinearity with off_epa, and `rest_diff`, which the calibration dropped
    outright. Rest is published for FUTURE games -- games.csv carries
    home_rest/away_rest before a snap -- so a week-1 matchup with no play data
    of any kind still had a non-None `rest_diff`. Reading that as "tier 0 has
    something" suppressed the fallbacks, and since rest carries no weight it
    then contributed nothing either: every week-1 game scored 0 and read "No
    clear lean", which is exactly the state the fallbacks exist to end. Keying
    on the weights makes the rule mean what it says, and keeps meaning it if a
    signal is later dropped or restored.

    If any weighted non-fallback signal survived, every fallback is dropped.
    Otherwise the first tier in _FALLBACK_TIERS with a value keeps it and the
    rest go. Returns a new dict; the caller's is left alone."""
    out = dict(sig)
    weighted = set(weights or ())
    if any(out.get(k) is not None for k in weighted if k not in _FALLBACK_SIGNALS):
        chosen = ()
    else:
        chosen = next((tier for tier in _FALLBACK_TIERS
                       if any(out.get(k) is not None for k in tier)), ())
    for k in _FALLBACK_SIGNALS:
        if k not in chosen:
            out[k] = None
    return out


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
        # TIER GATE PER BET TYPE, and BEFORE the availability override below.
        # Per bet type because tier 0 is defined by this market's own weights;
        # before the override because a QB-out game with real EPA on file is
        # NOT in cold start, and gating afterwards would drop off_epa, empty
        # tier 0, and hand a calibrated matchup to last season's margin.
        bt_sig = {k: _apply_fallback_tiers(sig, w).get(k) for k in w}

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
                 away_rest, home_rest,
                 away_season_margin=None, home_season_margin=None,
                 away_prior_margin=None, home_prior_margin=None):
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
        # Default None so every existing caller (and nfl_backtest.py) keeps its
        # current behaviour exactly: absent means the signal is absent, so a
        # caller that passes neither gets the pre-fallback lean unchanged.
        "away_season_margin": _coerce(away_season_margin),
        "home_season_margin": _coerce(home_season_margin),
        "away_prior_margin": _coerce(away_prior_margin),
        "home_prior_margin": _coerce(home_prior_margin),
    }
