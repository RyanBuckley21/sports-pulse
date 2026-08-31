"""EPL's cold-start tier: the rule that decides what August scores on.

THE GAP IT CLOSES. Form never crosses a season boundary -- promotion and
relegation turn over three clubs a summer, so last season's table is a
different league -- which made every August a cold start. Below
epl_signals.MIN_MATCHES, score_game returned {} for every fixture: no lean, no
Signal Score, nothing to bet, through the whole of August and most of
September, every season. EPL was the last active sport producing no scores at
all.

ONE TIER, not the two CFB and NFL carry, and the asymmetry is deliberate. Those
sports need a mid-season fallback because their calibrated signals can vanish
for reasons unrelated to the calendar (no CFBD budget, an unjoinable schedule
source, an unpublished nflverse release). EPL's inputs come from the same ESPN
scoreboard as its fixtures, so above the gate the weighted model is always
there. MIN_MATCHES = 5 already puts the handoff at match 6, which is where the
measurement puts it.

WHAT IS PINNED HERE, all of it silent when wrong:

  THE {} CONTRACT SURVIVES. A cold match whose fallback is ALSO empty -- a
  promoted club, with no prior Premier League season -- must still return {},
  the contract this function shipped with and the one epl_backtest relies on.
  Without it the store fills with two markets reading "No clear lean" at score
  0, which renders identically and is pure noise.

  ABOVE THE GATE NOTHING CHANGES. The weighted lean must be byte-identical with
  and without a prior-season number attached, even one pointing hard the other
  way.

  BOTH MARKETS, ONE LEAN. EPL scores double_chance and match_result off the
  same lean at different bars (55 and 75), and the fallback has to feed both --
  measured at 82.9% and 71.4% respectively in the cold-start window.

  THE CARD CANNOT LIE. Unlike CFB and NFL, an EPL club under the gate usually
  HAS played -- one or two matches -- so the card would print "3.00 goals per
  match" off a single 3-0 beside a lean that deliberately ignored it. The
  prior-season row leads, and the in-season rows carry their denominator.

No network: every input is a number handed straight to score_game. Whether the
picks win is a backtest, and those numbers live in config.yaml's comments and
epl_signals._FALLBACK_SIGNALS.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

import epl_signals  # noqa: E402
from fetchers import epl as epl_fetcher  # noqa: E402

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
    base = dict(away_abbr="AWY", home_abbr="HME", away_played=0, home_played=0,
                away_gf_pm=None, home_gf_pm=None, away_ga_pm=None, home_ga_pm=None,
                away_ppm=None, home_ppm=None, away_gd_pm=None, home_gd_pm=None,
                away_recent_ppm=None, home_recent_ppm=None,
                away_recent_gd_pm=None, home_recent_gd_pm=None,
                away_venue_ppm=None, home_venue_ppm=None,
                away_rest=None, home_rest=None,
                away_prior_gd_pm=None, home_prior_gd_pm=None)
    base.update(kw)
    return epl_signals.score_game(CONFIG, "epl", epl_signals.build_inputs(**base))


def flat(scored):
    return {k: (v["side"], v["score"]) for k, v in scored.items()}


WARM = dict(away_played=12, home_played=12,
            away_gf_pm=1.0, home_gf_pm=2.1, away_ga_pm=1.7, home_ga_pm=0.8,
            away_recent_ppm=0.9, home_recent_ppm=2.3,
            away_venue_ppm=0.9, home_venue_ppm=2.3)

# ------------------------------------------------------- the {} contract
ok("a cold match with NO prior season returns {} (the promoted-club case)",
   score() == {}, score())
ok("  ...still {} even with one side's prior GD known",
   score(home_prior_gd_pm=1.2) == {}, score(home_prior_gd_pm=1.2))
ok("  ...and with only the away side's",
   score(away_prior_gd_pm=1.2) == {}, score(away_prior_gd_pm=1.2))

# ---------------------------------------------------- the cold-start lean
cold = score(away_prior_gd_pm=-0.8, home_prior_gd_pm=1.1)
ok("a cold match WITH both prior GDs scores", bool(cold), cold)
ok("  on both markets", set(cold) == {"double_chance", "match_result"}, sorted(cold))
ok("  leaning to the better prior season", cold["double_chance"]["side"].startswith("HME"),
   flat(cold))
ok("  and double chance says 'or Draw'", "or Draw" in cold["double_chance"]["side"],
   cold["double_chance"]["side"])
ok("  while match_result names the side outright",
   cold["match_result"]["side"] in ("HME", "AWY", "No clear lean"), cold["match_result"]["side"])
mirrored = score(away_prior_gd_pm=1.1, home_prior_gd_pm=-0.8)
ok("the mirror image leans the other way", mirrored["double_chance"]["side"].startswith("AWY"),
   flat(mirrored))
ok("  and scores the same magnitude",
   mirrored["double_chance"]["score"] == cold["double_chance"]["score"])

scores = [score(away_prior_gd_pm=0.0, home_prior_gd_pm=g)["double_chance"]["score"]
          for g in (0.0, 0.2, 0.5, 1.0, 1.8, 3.0)]
ok("score is monotone in the gap", scores == sorted(scores), scores)
ok("  and saturates at 100", max(scores) <= 100, scores)

# PER-MARKET BARS. The whole point of scoring two markets off one lean.
mid = score(away_prior_gd_pm=-0.35, home_prior_gd_pm=0.35)
ok("a mid lean clears double chance but not the outright market",
   mid["double_chance"]["side"] != "No clear lean"
   and mid["match_result"]["side"] == "No clear lean", flat(mid))
big = score(away_prior_gd_pm=-1.5, home_prior_gd_pm=1.5)
ok("  a big one clears both", big["double_chance"]["side"] != "No clear lean"
   and big["match_result"]["side"] != "No clear lean", flat(big))

# ------------------------------------------------ above the gate: no change
warm = score(**WARM)
ok("a warm match scores on the weighted signals", bool(warm), flat(warm))
ok("attaching a prior GD changes NOTHING above the gate",
   score(away_prior_gd_pm=-0.9, home_prior_gd_pm=1.4, **WARM) == warm, flat(warm))
ok("  ...even pointing hard the other way",
   score(away_prior_gd_pm=3.0, home_prior_gd_pm=-3.0, **WARM) == warm, flat(warm))
ok("a caller passing no prior GD at all is unaffected (epl_backtest's path)",
   score(**WARM) == warm)

# The gate is a floor on the LOWER side, not an average.
just_under = dict(WARM); just_under["away_played"] = epl_signals.MIN_MATCHES - 1
ok("one side under the gate makes the whole match cold",
   score(away_prior_gd_pm=-0.8, home_prior_gd_pm=1.1, **just_under)
   == score(away_prior_gd_pm=-0.8, home_prior_gd_pm=1.1),
   flat(score(away_prior_gd_pm=-0.8, home_prior_gd_pm=1.1, **just_under)))
just_over = dict(WARM); just_over["away_played"] = epl_signals.MIN_MATCHES
ok("  and exactly at the gate it is warm",
   score(away_prior_gd_pm=3.0, home_prior_gd_pm=-3.0, **just_over)
   == score(**just_over))

# ------------------------------------------------------------- config wiring
cfg = CONFIG["betting_signals"]["epl"]
ok("config carries the prior_gd scale", "prior_gd_gap" in cfg["scales"], sorted(cfg["scales"]))
ok("  and it is a real dispersion", cfg["scales"]["prior_gd_gap"] > 0.1)
for bt in ("double_chance", "match_result"):
    ok("{} weights prior_gd".format(bt), cfg["bet_types"][bt].get("prior_gd"))
ok("every SIGNAL_SPECS entry has a configured scale",
   all(v["scale_key"] in cfg["scales"] for v in epl_signals.SIGNAL_SPECS.values()))
ok("prior_gd is a declared spec",
   all(k in epl_signals.SIGNAL_SPECS for k in epl_signals._FALLBACK_SIGNALS))

# -------------------------------------------------- promoted clubs excluded
FULL = [{"home": {"id": "A", "score": 2}, "away": {"id": "B", "score": 0}}] * 19
gd = epl_fetcher.build_prior_season_gd(FULL)
ok("a club with a full prior season carries a GD", gd.get("A") == 2.0, gd)
ok("  and its opponent the negative of it", gd.get("B") == -2.0, gd)
short = epl_fetcher.build_prior_season_gd(FULL[:18])
ok("a club short of the floor is EXCLUDED -- the promoted-club case",
   short == {}, short)
ok("the floor is half a season", epl_fetcher.PRIOR_SEASON_MIN_MATCHES == 19)

# -------------------------------------------------------- the card cannot lie
thin = {"played": 1, "gf_pm": 3.0, "ga_pm": 0.0, "recent_ppm": 3.0}
rows = epl_fetcher._display_signals({"abbr": "AWY"}, {"abbr": "HME"},
                                    thin, thin, -0.8, 1.1, cold=True)
labels = [r["label"] for r in rows]
ok("the cold card leads with the row the lean used",
   labels and labels[0].startswith("Last season goal difference"), labels)
ok("  and every in-season row carries its denominator",
   all("(1 played)" in l for l in labels[1:]), labels)
warm_rows = epl_fetcher._display_signals({"abbr": "AWY"}, {"abbr": "HME"},
                                         dict(thin, played=12), dict(thin, played=12),
                                         -0.8, 1.1, cold=False)
wl = [r["label"] for r in warm_rows]
ok("a warm card shows no prior-season row",
   not any(l.startswith("Last season") for l in wl), wl)
ok("  and no denominators", not any("played)" in l for l in wl), wl)

print("epl cold start: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "epl cold start: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
