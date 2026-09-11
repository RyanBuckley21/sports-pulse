"""CFB's fallback signal tiers, offline and deterministic.

THE PROPERTY UNDER TEST, and why it is worth a file: a calibrated lean must
never contain a fallback signal, and a fallback lean must never contain more
than one tier. Both failures are silent. Letting `prior_margin` into a November
lean would quietly drag last season into a calibrated answer -- the config
weights were measured without it, so every mid-season score would shift and
nothing would say so. Letting both margin tiers in at once would double-count
the same quantity measured over two windows.

WHY FALLBACKS EXIST AT ALL. Before them, a game with no CFBD team form scored 0
and read "No clear lean". That is every week-0 and week-1 game, and every game
of a season running on the ESPN fallback schedule -- which is the state the
2026 season is in. Both tiers are points margin per game off the plain
schedule, so they cost no CFBD calls; see cfb_signals._FALLBACK_TIERS for the
walk-forward measurement that put the handoff between them at week 4.

NO NETWORK AND NO FIXTURE. Every input here is a number handed straight to
score_game, because what is being tested is the gating rule, not the parsing of
anybody's feed. The real-data question -- does the resulting pick win -- is a
backtest, not a unit test, and the numbers from that live in config.yaml's
comments and cfb_signals._FALLBACK_TIERS.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

import cfb_signals  # noqa: E402
import signal_core  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(REPO, "config.yaml")) as f:
    CONFIG = yaml.safe_load(f)

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


def score(**kw):
    base = dict(away_abbr="AWAY", home_abbr="HOME",
                away_off_ppa=None, home_off_ppa=None,
                away_def_ppa_allowed=None, home_def_ppa_allowed=None,
                away_turnover_diff=None, home_turnover_diff=None,
                away_season_margin=None, home_season_margin=None,
                away_prior_margin=None, home_prior_margin=None)
    base.update(kw)
    return cfb_signals.score_game(CONFIG, "cfb", cfb_signals.build_inputs(**base))["moneyline"]


PPA = dict(away_off_ppa=0.05, home_off_ppa=0.35,
           away_def_ppa_allowed=0.30, home_def_ppa_allowed=0.10,
           away_turnover_diff=-0.5, home_turnover_diff=1.0)


# ------------------------------------------------- the calibrated tier wins
# The strongest form of "fallbacks do not perturb a calibrated lean": the same
# PPA inputs, with and without both margin tiers attached, must produce the
# identical dict. No golden value to maintain, and it cannot pass by accident.
plain = score(**PPA)
ok("a calibrated lean is unchanged by an attached season margin",
   score(away_season_margin=-30.0, home_season_margin=30.0, **PPA) == plain, plain)
ok("  ...by an attached prior margin",
   score(away_prior_margin=-30.0, home_prior_margin=30.0, **PPA) == plain, plain)
ok("  ...by both at once",
   score(away_season_margin=-30.0, home_season_margin=30.0,
         away_prior_margin=-30.0, home_prior_margin=30.0, **PPA) == plain, plain)

# And the fallbacks are not merely outvoted -- they are dropped. Point them the
# OPPOSITE way at full strength: an averaged-in signal would move the score.
opposed = score(away_season_margin=40.0, home_season_margin=-40.0,
                away_prior_margin=40.0, home_prior_margin=-40.0, **PPA)
ok("  ...even when both point the other way at full strength", opposed == plain,
   "{} vs {}".format(opposed, plain))

# One surviving PPA signal is still the calibrated tier.
ok("a SINGLE surviving calibrated signal still suppresses the fallbacks",
   score(away_off_ppa=0.05, home_off_ppa=0.35,
         away_prior_margin=40.0, home_prior_margin=-40.0)
   == score(away_off_ppa=0.05, home_off_ppa=0.35))


# ----------------------------------------------------- tier order among the two
both = score(away_season_margin=-5.0, home_season_margin=5.0,
             away_prior_margin=40.0, home_prior_margin=-40.0)
season_only = score(away_season_margin=-5.0, home_season_margin=5.0)
ok("season margin beats prior margin when both exist", both == season_only,
   "{} vs {}".format(both, season_only))
ok("  and prior margin really would have said otherwise",
   score(away_prior_margin=40.0, home_prior_margin=-40.0)["side"] == "AWAY")
ok("  (so that was not a no-op)", season_only["side"] == "HOME", season_only)

prior_only = score(away_prior_margin=-20.0, home_prior_margin=10.0)
ok("prior margin is used when nothing else is", prior_only["side"] == "HOME", prior_only)
ok("  and scores something", prior_only["score"] > 0, prior_only)


# --------------------------------------------------------------- no signals
blank = score()
ok("no signals at all is still 'No clear lean'", blank["side"] == "No clear lean", blank)
ok("  scoring 0", blank["score"] == 0, blank)


# ------------------------------------------------------------- direction
ok("a better season margin leans that way (home)",
   score(away_season_margin=-14.0, home_season_margin=14.0)["side"] == "HOME")
ok("  and the mirror image leans the other way",
   score(away_season_margin=14.0, home_season_margin=-14.0)["side"] == "AWAY")
ok("a better prior margin leans that way (home)",
   score(away_prior_margin=-14.0, home_prior_margin=14.0)["side"] == "HOME")
ok("  and the mirror image leans the other way",
   score(away_prior_margin=14.0, home_prior_margin=-14.0)["side"] == "AWAY")

# Symmetry: the same gap must produce the same magnitude either way round --
# AT A NEUTRAL SITE. At a real home game it must NOT, because the home-field
# adjustment deliberately breaks that symmetry (see the home_field section at
# the end of this file). Asserting symmetry unconditionally is what this check
# used to do, and it would now pass only if the adjustment had quietly stopped
# being applied.
h = score(away_season_margin=-7.0, home_season_margin=7.0, neutral_site=True)
a = score(away_season_margin=7.0, home_season_margin=-7.0, neutral_site=True)
ok("equal and opposite gaps score equally at a neutral site",
   h["score"] == a["score"], "{} vs {}".format(h["score"], a["score"]))

# Monotone: a wider gap can never score lower.
scores = [score(away_season_margin=0.0, home_season_margin=g)["score"]
          for g in (0, 3, 7, 14, 21, 35, 60)]
ok("score is monotone in the gap", scores == sorted(scores), scores)
ok("  and saturates at 100 rather than exceeding it", max(scores) <= 100, scores)

# A one-sided input is not a gap. Both sides must be present or the signal is
# absent -- otherwise a team missing from the margin table would read as 0.0
# and manufacture a lean out of nothing.
ok("a one-sided season margin is not a signal",
   score(home_season_margin=20.0)["side"] == "No clear lean",
   score(home_season_margin=20.0))
ok("a one-sided prior margin is not a signal",
   score(away_prior_margin=20.0)["side"] == "No clear lean",
   score(away_prior_margin=20.0))


# ------------------------------------------------------------ config wiring
cfg = CONFIG["betting_signals"]["cfb"]
for key in ("season_margin_gap", "prior_margin_gap"):
    ok("config carries the {} scale".format(key), key in cfg["scales"], sorted(cfg["scales"]))
    ok("  and it is a real dispersion, not a placeholder", cfg["scales"][key] > 1)
for key in ("season_margin", "prior_margin"):
    ok("config weights {}".format(key), cfg["bet_types"]["moneyline"].get(key), None)
ok("every SIGNAL_SPECS entry has a configured scale",
   all(s["scale_key"] in cfg["scales"] for s in cfb_signals.SIGNAL_SPECS.values()),
   sorted(cfg["scales"]))
ok("every fallback signal is a declared spec",
   all(k in cfb_signals.SIGNAL_SPECS for k in cfb_signals._FALLBACK_SIGNALS))
ok("no fallback signal appears in more than one tier",
   len(cfb_signals._FALLBACK_SIGNALS) == len(set(cfb_signals._FALLBACK_SIGNALS)))


# ---------------------------------------------------------- backward compat
# cfb_backtest.py and any other existing caller build inputs WITHOUT the two
# new keyword arguments. That must keep producing exactly the pre-fallback
# lean, or a calibration would be measuring a different model than production.
legacy = cfb_signals.build_inputs(
    away_abbr="AWAY", home_abbr="HOME",
    away_off_ppa=0.05, home_off_ppa=0.35,
    away_def_ppa_allowed=0.30, home_def_ppa_allowed=0.10,
    away_turnover_diff=-0.5, home_turnover_diff=1.0)
ok("a caller passing no margins gets the calibrated lean unchanged",
   cfb_signals.score_game(CONFIG, "cfb", legacy)["moneyline"] == plain)
ok("  and its inputs carry the new keys as None",
   legacy["home_season_margin"] is None and legacy["home_prior_margin"] is None)


# ------------------------------------------------------- home-field adjustment
# THE PROPERTY: a points-margin gap carries no venue term of its own, so the
# fallback tiers scored a team identically whether it was at home or on the
# road -- and the same Signal Score therefore meant two different things.
# Measured over 2015-2025, a CFB road lean at the same score as a home lean hit
# 17.7pp worse on the prior tier. The fix (config.betting_signals.cfb.
# home_field, evidence in that block's comments) shifts the HOME side's margin
# input, and only at a real home game.
hf = cfg.get("home_field") or {}
ok("config carries a home_field block", bool(hf), hf)
for key in ("season_margin", "prior_margin"):
    ok("  with a {} shift".format(key), (hf.get(key) or 0) > 0, hf)
ok("  and no shift on any calibrated PPA signal -- never measured, never asserted",
   not any(k in hf for k in ("off_ppa", "def_ppa_allowed", "turnover_diff")), hf)

# A DEAD-EVEN MATCHUP is the check that actually bites. With no venue term it
# is "No clear lean" by construction, so anything it scores at all is the
# home-field shift and nothing else -- delete the home_field lookup from
# cfb_signals._base_signals and the first of these two flips straight back.
even_home = score(away_season_margin=0.0, home_season_margin=0.0)
even_neutral = score(away_season_margin=0.0, home_season_margin=0.0, neutral_site=True)
ok("an even matchup at home scores toward HOME on the venue alone",
   even_home["score"] > 0, even_home)
ok("  and the same matchup at a neutral site scores nothing at all",
   even_neutral["score"] == 0, even_neutral)
# It scores, but it does NOT clear min_threshold on its own (27 against a bar
# of 40), which is the right behaviour and worth pinning: home field is a real
# input, not a reason to publish a pick on an otherwise even game. The prior
# tier's larger shift DOES clear it -- see below -- because that tier's inputs
# are compressed enough that the same points of home edge move the lean
# further.
ok("  and venue alone is not enough to publish a season-tier pick",
   even_home["side"] == "No clear lean", even_home)

# The shift is worth exactly what config says, in the signal's own units: shift
# the home team DOWN by the configured amount and the answer must land back on
# the neutral-site one.
offset = score(away_season_margin=0.0, home_season_margin=-hf["season_margin"])
ok("the applied shift equals the configured one",
   offset["score"] == even_neutral["score"] and offset["side"] == even_neutral["side"],
   "{} vs {}".format(offset, even_neutral))

# PER-SIGNAL, not one global constant -- the prior tier carries its own,
# larger shift because a full prior season regresses harder to the mean.
prior_even = score(away_prior_margin=0.0, home_prior_margin=0.0)
prior_offset = score(away_prior_margin=0.0, home_prior_margin=-hf["prior_margin"])
ok("the prior tier uses its own shift, not the season tier's",
   prior_offset["side"] == "No clear lean", prior_offset)
ok("  and that tier's shift is large enough to lean home on venue alone",
   prior_even["side"] == "HOME", prior_even)
ok("  the two shifts are genuinely different numbers",
   hf["season_margin"] != hf["prior_margin"], hf)

# DIRECTION: it can only ever help the home side, never the road one.
road_favoured = score(away_season_margin=10.0, home_season_margin=0.0)
road_neutral = score(away_season_margin=10.0, home_season_margin=0.0, neutral_site=True)
ok("playing at home cuts the road side's score, never the home side's",
   road_favoured["score"] < road_neutral["score"], 
   "{} vs {}".format(road_favoured, road_neutral))
# And this is the effect the measurement predicted: a road lean that used to
# clear the bar on a 10-point margin edge no longer does. Road picks get rarer
# and, per the backtest, better.
ok("  and a marginal road lean stops clearing the bar",
   road_neutral["side"] == "AWAY" and road_favoured["side"] == "No clear lean",
   "{} vs {}".format(road_favoured, road_neutral))

# THE CALIBRATED TIER IS UNTOUCHED. `plain` was scored at the top of this file
# and must still be bit-for-bit what it always was, at either venue.
ok("the calibrated PPA lean is unchanged by venue",
   cfb_signals.score_game(CONFIG, "cfb", cfb_signals.build_inputs(
       away_abbr="AWAY", home_abbr="HOME",
       away_off_ppa=0.05, home_off_ppa=0.35,
       away_def_ppa_allowed=0.30, home_def_ppa_allowed=0.10,
       away_turnover_diff=-0.5, home_turnover_diff=1.0,
       neutral_site=True))["moneyline"] == plain, plain)

# signal_core.home_field's own contract, directly.
ok("home_field leaves None alone", signal_core.home_field(None, 5.0, False) is None)
ok("home_field ignores a zero shift", signal_core.home_field(3.0, 0.0, False) == 3.0)
ok("home_field ignores a missing shift", signal_core.home_field(3.0, None, False) == 3.0)
ok("home_field applies nothing at a neutral site", signal_core.home_field(3.0, 5.0, True) == 3.0)
ok("home_field adds the shift at a home game", signal_core.home_field(3.0, 5.0, False) == 8.0)


print("cfb signals: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "cfb signals: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
