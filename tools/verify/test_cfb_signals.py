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

# Symmetry: the same gap must produce the same magnitude either way round.
h = score(away_season_margin=-7.0, home_season_margin=7.0)
a = score(away_season_margin=7.0, home_season_margin=-7.0)
ok("equal and opposite gaps score equally", h["score"] == a["score"],
   "{} vs {}".format(h["score"], a["score"]))

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


print("cfb signals: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "cfb signals: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
