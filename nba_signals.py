"""NBA Betting Signal Layer -- deterministic per-game Signal Scores for the NBA
moneyline. Built 2026-11-11 on the owner's instruction ("Put NBA on there"),
from what nba_backtest.py measured rather than from NFL's shape by habit.

ONE SIGNAL, AND ONE FALLBACK, because that is what the measurement supports
(nba_backtest.py, 3,690 decided games 2023-26, CLAUDE.md "Add NBA"):

  season_margin   the two clubs' points margin per game THIS season, once
                  both have played 5. r = +0.391 with the home margin; picks
                  the winner 65.7% against always-home's 54.8% on the same
                  games. The 5-game floor must not be raised to "improve"
                  it -- the gap to always-home widens with the floor only
                  because the baseline decays.
  prior_margin    last season's margin, used ONLY while a matchup has no
                  season_margin yet (the first two weeks). r = +0.251.

  Measured and deliberately NOT here: last-10 margin (r = +0.806 with
  season_margin, and where the two disagree season margin is right 51.8% -- a
  coin flip, so it adds nothing) and rest difference (does not beat
  always-home: -0.1pp [-3.3, +3.2]).

THE SCORE SHAPE is signal_core's, shared with NFL and CFB: tanh(gap / scale)
toward home, x100, a side named at or above the threshold. Every scale, home
shift and threshold lives in config.yaml's betting_signals.nba block with its
derivation (nba_backtest.py --fit); nothing is asserted here.

HOME COURT is a shift on the home club's margin, in points, fitted (OLS of
the home margin on the tier's own gap, intercept / slope) and never the ~3
points people quote: pooled over 3,678 true-home games it is +1.854
[+1.316, +2.370]. A neutral site (Mexico City, Paris, the Cup semifinals)
gets no shift.

NO AVAILABILITY OVERRIDE. NFL penalises a side whose quarterback is ruled
out. The NBA equivalent -- a star resting -- is announced hours before tip on
an injury report this pipeline does not read, and no free feed for it has
been verified. A lean here therefore does not know who is playing tonight.
That is a real limitation of the pick, stated on the card (fetchers.nba's
caution note), not something this module pretends to handle.

PRICE-BLIND (invariant 1): no odds, line or market number is an input.

ACCURACY IS NOT EDGE. Whether these leans beat the market cannot be measured:
the NBA has no price history on this feed, and data/nba_prices.jsonl only
began on 2026-11-11. NFL's version of this model, which CAN be measured, knows
nothing its closing price does not (nfl_winprob.py). Assume the same here
until a season of captured prices says otherwise.
"""

import signal_core
from signal_core import (coerce as _coerce, finalize as _finalize,
                         paired as _paired, raw_lean as _raw_lean)

# The single place each signal's inputs, scale key and direction are defined.
# score_game reads it for the live lean and nba_backtest.py --fit reads the
# SAME table for the raw gap it calibrates, so the two cannot drift.
SIGNAL_SPECS = {
    "season_margin": {"home_key": "home_season_margin", "away_key": "away_season_margin",
                      "scale_key": "season_margin_gap", "favors": "higher"},
    "prior_margin": {"home_key": "home_prior_margin", "away_key": "away_prior_margin",
                     "scale_key": "prior_margin_gap", "favors": "higher"},
}

# Best tier first. A matchup scores on the first tier it has a value for and
# on nothing below it: last season's margin must not dilute this season's the
# moment both exist (NFL's _apply_fallback_tiers, same rule, same reason).
TIERS = (("season_margin",), ("prior_margin",))

_MARKET_PRECEDENCE = ("moneyline",)


def build_inputs(away_abbr, home_abbr,
                 away_season_margin=None, home_season_margin=None,
                 away_prior_margin=None, home_prior_margin=None,
                 neutral_site=False):
    """The deterministic input dict for one game. A margin is None when the
    club has not earned one (under the games floor; no prior season)."""
    return {
        "away_abbr": away_abbr, "home_abbr": home_abbr,
        "neutral_site": bool(neutral_site),
        "away_season_margin": _coerce(away_season_margin),
        "home_season_margin": _coerce(home_season_margin),
        "away_prior_margin": _coerce(away_prior_margin),
        "home_prior_margin": _coerce(home_prior_margin),
    }


def base_signals(inp, scales, home_field=None):
    """Each signal's directional value toward HOME in [-1, 1], or None where
    either club's input is missing. `home_field` is {signal: points added to
    the home club's margin}; a neutral site gets none (signal_core.home_field)."""
    hf = home_field or {}
    neutral = bool(inp.get("neutral_site"))
    return {name: _paired(signal_core.home_field(inp.get(spec["home_key"]),
                                                 hf.get(name), neutral),
                          inp.get(spec["away_key"]),
                          scales[spec["scale_key"]], spec["favors"])
            for name, spec in SIGNAL_SPECS.items()}


def active_tier(sig):
    """The names of the best tier that has a value, or () when none does."""
    return next((tier for tier in TIERS if any(sig.get(k) is not None for k in tier)), ())


def score_game(config, sport_key, inputs):
    """{bet_type: {side, score, flags, tier}} for one game (moneyline only).
    {} when the sport has no betting_signals block -- which is how an
    unconfigured NBA stays silent rather than scoring on defaults."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    bet_types = cfg.get("bet_types") or {}
    if not bet_types:
        return {}
    sig = base_signals(inputs, cfg["scales"], cfg.get("home_field"))
    tier = active_tier(sig)
    labels = (inputs.get("home_abbr"), inputs.get("away_abbr"))
    out = {}
    for bt, weights in bet_types.items():
        used = {k: sig.get(k) for k in weights if k in tier}
        lean, n, agree = _raw_lean(used, {k: weights[k] for k in used})
        entry = _finalize(lean, n, agree, cfg.get("min_threshold", 0), labels)
        # Which tier produced the lean, so the card can say "on last season's
        # numbers" while that is true. Not a flag: flags are availability.
        entry["tier"] = tier[0] if tier and lean is not None else None
        out[bt] = entry
    return out


def list_markets(scored):
    """Every market with a real lean, as [{bet_type, side, score, flags}],
    best score first. One market today; the shape matches the other sports'."""
    prec = {k: i for i, k in enumerate(_MARKET_PRECEDENCE)}
    rows = []
    for bt, entry in (scored or {}).items():
        if isinstance(entry, dict) and entry.get("side") and entry["side"] != "No clear lean":
            rows.append((bt, entry["side"], entry.get("score", 0), entry.get("flags") or []))
    rows.sort(key=lambda c: (-c[2], prec.get(c[0], 99)))
    return [{"bet_type": bt, "side": side, "score": score, "flags": list(flags)}
            for bt, side, score, flags in rows]


def top_market(scored, threshold):
    return signal_core.top_market(list_markets(scored), threshold)
