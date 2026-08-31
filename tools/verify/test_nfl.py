"""NFL's fallback tiers and its grading rules, offline and deterministic.

Two groups, both covering things that fail silently rather than throwing.

FALLBACK TIERS. Before them, every week-1 game scored 0 / "No clear lean" --
nflverse publishes a season's stats_team release only once that season has
games, so the opening weekend has no EPA at all, every year. Two schedule-
derived margin tiers fill it. The property under test is that a calibrated lean
never contains one, and a fallback lean never contains more than one.

THE BUG THAT MADE THIS FILE NECESSARY, and the assertion that pins it: tier 0
is "the signals this bet type WEIGHTS", not "every declared spec". NFL declares
two specs carrying no weight -- `scoring_margin` (excluded for collinearity
with off_epa) and `rest_diff` (dropped by the calibration) -- and games.csv
publishes home_rest/away_rest for FUTURE games. So a week-1 matchup with no
play data of any kind still had a non-None rest_diff; reading that as "tier 0
has something" suppressed both fallbacks, and rest carries no weight so it
contributed nothing in their place. Every opening-weekend game scored 0, which
is the exact state the fallbacks exist to end. It was caught by building the
real 2026 opener, not by reasoning.

GRADING. A TIE IS A PUSH. NFL overtime need not produce a winner and about one
game a season ends level; every book returns the stake. cfb_grading returns
UNRESOLVED for the same score line, because college football abolished ties in
1996 and there it means the feed is broken -- same shape, opposite meaning.
Grading a tie MISS is a quiet once-a-season wrong verdict in an append-only
ledger. And THE STORE'S KEY IS NOT THE FEED'S KEY: the store is keyed by
nflverse's game_id ("2026_01_DAL_PHI"), which ESPN has never heard of, so
fetch_slate re-keys the scoreboard through games.csv's `espn` column.

`nfl_games_fixture.json` is REAL ESPN data captured from live responses across
five dates -- 11 games including overtime finals and the genuine 40-40
GB-at-DAL tie of 2025-09-28, found by scanning games.csv for result=0 rather
than hoping one turned up. Real rather than hand-written because the shape of
an overtime and a tied final is exactly what is being parsed. PENDING and
POSTPONED are built by editing a real event's status block; no postponed game
appeared on any date sampled, and the edit touches only the field those
branches read.
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

import nfl_grading  # noqa: E402
import nfl_signals  # noqa: E402
import signal_report as sr  # noqa: E402
from fetchers import nfl as nfl_fetcher  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
with open(os.path.join(REPO, "config.yaml")) as f:
    CONFIG = yaml.safe_load(f)
with open(os.path.join(HERE, "nfl_games_fixture.json")) as f:
    EV = {str(e["id"]): e for e in json.load(f)}

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


# ========================================================= fallback tiers
def score(**kw):
    base = dict(away_abbr="AWY", home_abbr="HME",
                away_off_epa=None, home_off_epa=None,
                away_def_epa_allowed=None, home_def_epa_allowed=None,
                away_turnover_diff=None, home_turnover_diff=None,
                away_scoring_margin=None, home_scoring_margin=None,
                away_rest=None, home_rest=None,
                away_season_margin=None, home_season_margin=None,
                away_prior_margin=None, home_prior_margin=None)
    base.update(kw)
    return nfl_signals.score_game(CONFIG, "nfl", nfl_signals.build_inputs(**base))["moneyline"]


EPA = dict(away_off_epa=-0.05, home_off_epa=0.15,
           away_def_epa_allowed=0.10, home_def_epa_allowed=-0.05,
           away_turnover_diff=-0.5, home_turnover_diff=0.6)

plain = score(**EPA)
ok("a calibrated lean is unchanged by an attached season margin",
   score(away_season_margin=-20.0, home_season_margin=20.0, **EPA) == plain, plain)
ok("  ...by an attached prior margin",
   score(away_prior_margin=-20.0, home_prior_margin=20.0, **EPA) == plain, plain)
ok("  ...even with both pointing the other way at full strength",
   score(away_season_margin=25.0, home_season_margin=-25.0,
         away_prior_margin=25.0, home_prior_margin=-25.0, **EPA) == plain, plain)

# THE REGRESSION. rest_diff and scoring_margin are declared specs with NO
# weight; neither may look like in-season evidence.
rest_only = score(away_rest=7.0, home_rest=10.0,
                  away_prior_margin=-12.0, home_prior_margin=12.0)
ok("an unweighted rest_diff does NOT suppress the fallbacks",
   rest_only["side"] == "HME" and rest_only["score"] > 0, rest_only)
ok("  and matches the same game with no rest at all",
   rest_only == score(away_prior_margin=-12.0, home_prior_margin=12.0), rest_only)
marg_only = score(away_scoring_margin=-3.0, home_scoring_margin=3.0,
                  away_prior_margin=-12.0, home_prior_margin=12.0)
ok("an unweighted scoring_margin does NOT suppress them either",
   marg_only == score(away_prior_margin=-12.0, home_prior_margin=12.0), marg_only)

# A single WEIGHTED signal still means tier 0.
ok("one surviving weighted signal DOES suppress the fallbacks",
   score(away_off_epa=-0.05, home_off_epa=0.15,
         away_prior_margin=25.0, home_prior_margin=-25.0)
   == score(away_off_epa=-0.05, home_off_epa=0.15))

both = score(away_season_margin=-4.0, home_season_margin=4.0,
             away_prior_margin=25.0, home_prior_margin=-25.0)
season_only = score(away_season_margin=-4.0, home_season_margin=4.0)
ok("season margin beats prior margin when both exist", both == season_only, both)
ok("  and prior really would have said otherwise",
   score(away_prior_margin=25.0, home_prior_margin=-25.0)["side"] == "AWY")
ok("prior margin is used when nothing else is",
   score(away_prior_margin=-15.0, home_prior_margin=8.0)["side"] == "HME")

blank = score()
ok("no signals at all is still 'No clear lean'", blank["side"] == "No clear lean", blank)
ok("  scoring 0", blank["score"] == 0, blank)

ok("equal and opposite gaps score equally",
   score(away_season_margin=-6.0, home_season_margin=6.0)["score"]
   == score(away_season_margin=6.0, home_season_margin=-6.0)["score"])
scores = [score(away_season_margin=0.0, home_season_margin=g)["score"]
          for g in (0, 2, 5, 10, 18, 30, 60)]
ok("score is monotone in the gap", scores == sorted(scores), scores)
ok("  and saturates at 100", max(scores) <= 100, scores)
ok("a one-sided season margin is not a signal",
   score(home_season_margin=15.0)["side"] == "No clear lean")
ok("a one-sided prior margin is not a signal",
   score(away_prior_margin=15.0)["side"] == "No clear lean")

# The QB-out override must not be able to force a cold start.
qb = nfl_signals.score_game(
    CONFIG, "nfl",
    nfl_signals.build_inputs(
        away_abbr="AWY", home_abbr="HME",
        away_off_epa=-0.05, home_off_epa=0.15,
        away_def_epa_allowed=0.10, home_def_epa_allowed=-0.05,
        away_turnover_diff=-0.5, home_turnover_diff=0.6,
        away_scoring_margin=None, home_scoring_margin=None,
        away_rest=None, home_rest=None,
        away_prior_margin=25.0, home_prior_margin=-25.0),
    availability={"home_qb_out": True})["moneyline"]
no_prior = nfl_signals.score_game(
    CONFIG, "nfl",
    nfl_signals.build_inputs(
        away_abbr="AWY", home_abbr="HME",
        away_off_epa=-0.05, home_off_epa=0.15,
        away_def_epa_allowed=0.10, home_def_epa_allowed=-0.05,
        away_turnover_diff=-0.5, home_turnover_diff=0.6,
        away_scoring_margin=None, home_scoring_margin=None,
        away_rest=None, home_rest=None),
    availability={"home_qb_out": True})["moneyline"]
ok("a QB-out game with real EPA does NOT fall back to last season",
   qb == no_prior, "{} vs {}".format(qb, no_prior))

cfg = CONFIG["betting_signals"]["nfl"]
for key in ("season_margin_gap", "prior_margin_gap"):
    ok("config carries the {} scale".format(key), key in cfg["scales"])
    ok("  and it is a real dispersion", cfg["scales"][key] > 1)
ok("every SIGNAL_SPECS entry has a configured scale",
   all(v["scale_key"] in cfg["scales"] for v in nfl_signals.SIGNAL_SPECS.values()))
ok("every fallback signal is a declared spec",
   all(k in nfl_signals.SIGNAL_SPECS for k in nfl_signals._FALLBACK_SIGNALS))

# Existing callers (nfl_backtest) pass no margins and must be unaffected.
legacy = nfl_signals.build_inputs(
    away_abbr="AWY", home_abbr="HME",
    away_off_epa=-0.05, home_off_epa=0.15,
    away_def_epa_allowed=0.10, home_def_epa_allowed=-0.05,
    away_turnover_diff=-0.5, home_turnover_diff=0.6,
    away_scoring_margin=None, home_scoring_margin=None,
    away_rest=None, home_rest=None)
ok("a caller passing no margins gets the calibrated lean unchanged",
   nfl_signals.score_game(CONFIG, "nfl", legacy)["moneyline"] == plain)


# ================================================================ grading
def sides_of(e):
    return nfl_grading._sides(e)


def pick_for(e, side, bet_type="moneyline"):
    s = sides_of(e)
    return {"bet_type": bet_type, "side": side,
            "away_abbr": s["away"]["abbr"], "home_abbr": s["home"]["abbr"]}


finals = [e for e in EV.values() if nfl_grading.is_final(e)]
ok("the fixture carries finals", len(finals) >= 8, len(finals))

for e in finals:
    s = sides_of(e)
    hs, aws = s["home"]["score"], s["away"]["score"]
    if hs == aws:
        continue
    winner = s["home"]["abbr"] if hs > aws else s["away"]["abbr"]
    loser = s["away"]["abbr"] if hs > aws else s["home"]["abbr"]
    _t, v, b = nfl_grading.grade(pick_for(e, winner), e, False)
    ok("winner HITs ({} {}-{} {})".format(s["away"]["abbr"], aws, hs, s["home"]["abbr"]),
       v == "HIT" and b == "outcome", v)
    _t, v, _b = nfl_grading.grade(pick_for(e, loser), e, False)
    ok("  loser MISSes", v == "MISS", v)

ot = [e for e in EV.values()
      if "OT" in ((e["competitions"][0]["status"]["type"].get("detail")) or "")]
ok("the fixture carries real overtime finals", len(ot) >= 2, len(ot))
for e in ot:
    ok("  ESPN keeps name=STATUS_FINAL for OT",
       e["competitions"][0]["status"]["type"].get("name") == "STATUS_FINAL")
    ok("  is_final() accepts it anyway", nfl_grading.is_final(e) is True)

# A REAL TIE, not a constructed one.
ties = [e for e in finals if sides_of(e)["home"]["score"] == sides_of(e)["away"]["score"]]
ok("the fixture carries a REAL tied game", len(ties) == 1, len(ties))
for e in ties:
    s = sides_of(e)
    for who in (s["home"]["abbr"], s["away"]["abbr"]):
        text, v, b = nfl_grading.grade(pick_for(e, who), e, False)
        ok("a tie is a PUSH for {}".format(who), v == "PUSH", v)
        ok("  graded on the outcome, not left unresolved", b == "outcome", b)
        ok("  and says so in the text", "tie" in text.lower(), text)

# Unsettled paths.
sched = copy.deepcopy(finals[0])
sched["competitions"][0]["status"]["type"] = {
    "name": "STATUS_SCHEDULED", "completed": False, "description": "Scheduled",
    "shortDetail": "Sun, 1:00 PM ET"}
s = sides_of(sched)
text, v, _b = nfl_grading.grade(pick_for(sched, s["home"]["abbr"]), sched, False)
ok("an unplayed game is PENDING", v == "PENDING", v)
ok("  carrying ESPN's own status text", text == "Sun, 1:00 PM ET", text)
for name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED"):
    off = copy.deepcopy(sched)
    off["competitions"][0]["status"]["type"] = {
        "name": name, "completed": False, "description": name.split("_")[1].title()}
    _t, v, _b = nfl_grading.grade(pick_for(off, sides_of(off)["home"]["abbr"]), off, False)
    ok("{} is POSTPONED".format(name), v == "POSTPONED", v)

# Malformed.
f = finals[0]
_t, v, _b = nfl_grading.grade(pick_for(f, "ZZZ"), f, False)
ok("a side naming neither club is UNRESOLVED, not MISS", v == "UNRESOLVED", v)
_t, v, _b = nfl_grading.grade(
    pick_for(f, sides_of(f)["home"]["abbr"], bet_type="spread"), f, False)
ok("a market with no NFL rule is UNRESOLVED, not MISS", v == "UNRESOLVED", v)
_t, v, _b = nfl_grading.grade(pick_for(f, sides_of(f)["home"]["abbr"]), None, False)
ok("a game absent from the slate is UNRESOLVED", v == "UNRESOLVED", v)

# Adapter boundary and endpoint agreement.
import cfb_grading  # noqa: E402
import epl_grading  # noqa: E402
ok("nfl has an adapter", sr.adapter_for("nfl") is not None)
ok("  its grade is nfl_grading's", sr.SPORT_ADAPTERS["nfl"]["grade"] is nfl_grading.grade)
ok("  its store spans dates", sr.SPORT_ADAPTERS["nfl"]["store_spans_dates"] is True)
ok("MLB's grade is still MLB's own", sr.SPORT_ADAPTERS["mlb"]["grade"] is sr.grade)
ok("EPL's is still EPL's own", sr.SPORT_ADAPTERS["epl"]["grade"] is epl_grading.grade)
ok("CFB's is still CFB's own", sr.SPORT_ADAPTERS["cfb"]["grade"] is cfb_grading.grade)
ok("CFB still treats a tie as UNRESOLVED, not PUSH -- opposite rule, same shape",
   "UNRESOLVED" in open(os.path.join(REPO, "cfb_grading.py")).read().split("result == \"T\"")[1][:200])

ok("config.yaml has an nfl scoreboard_url", bool((CONFIG.get("nfl") or {}).get("scoreboard_url")))
ok("  and it equals the module constant",
   CONFIG["nfl"]["scoreboard_url"] == nfl_grading.ESPN_NFL_SCOREBOARD)
ok("  the grader reads config", nfl_grading._scoreboard_url(CONFIG) == nfl_grading.ESPN_NFL_SCOREBOARD)
ok("  and falls back without it", nfl_grading._scoreboard_url(None) == nfl_grading.ESPN_NFL_SCOREBOARD)

ok("the fetcher's window is a week", nfl_fetcher.FIXTURE_WINDOW_DAYS == 7)
ok("the season-margin floor is 5, not CFB's 3",
   nfl_fetcher.SEASON_MARGIN_MIN_GAMES == 5, nfl_fetcher.SEASON_MARGIN_MIN_GAMES)

print("nfl: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nfl: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f_ in failures:
    print("  FAIL " + f_)
sys.exit(1 if checks["fail"] else 0)
