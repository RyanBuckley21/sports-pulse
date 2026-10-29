"""CFB grading rules, offline and deterministic.

WHAT IS WORTH TESTING HERE, and why each group exists rather than being taken
on trust:

  THE TIE BRANCH. College football has had overtime since 1996, so a level
  final score does not mean the game ended level -- it means the source is
  wrong. Grading it MISS would write a confident wrong verdict into an
  append-only ledger. Nothing throws when this is backwards.

  THE OVERTIME STATUS. ESPN keeps name=STATUS_FINAL for an overtime game and
  puts the overtime in detail="Final/OT". A grader that matched the detail
  string would silently defer every OT result as PENDING -- again, no error,
  just a ledger that quietly stops recording a class of games. The fixture
  carries a real one (SMU 26-20 Miami) precisely so this is measured.

  THE TEAM-NAME JOIN. A pick's side carries fetchers/cfb._team_ref's
  abbreviation, which is NOT always ESPN's own `team.abbreviation`. Measured
  over 300 competitors across six real 2025 dates, they disagree for Air Force
  (AF vs AFA) and Buffalo (BUF vs BUFF) -- rare enough to survive any amount of
  spot-checking and to fail silently in November. Comparing the two
  vocabularies directly would grade every Air Force pick UNRESOLVED ("names
  neither program"), making a real record read as an empty one. Both sides go
  through _team_ref instead, and both of those games are in the fixture.

  THE ADAPTER BOUNDARY. MLB's and EPL's SPORT_ADAPTERS entries must still be
  their own functions -- the property that keeps a CFB change from reaching an
  MLB verdict.

  THE ENDPOINT AGREEMENT. config.yaml's cfb.scoreboard_url and
  fetchers/cfb.ESPN_CFB_SCOREBOARD are two copies of one URL, for two real
  consumers (grader and fallback schedule). This asserts they are equal, which
  is the only thing stopping a grader from reading a different endpoint than
  the builder.

`cfb_games_fixture.json` is REAL ESPN data captured from live responses across
four 2025 dates, trimmed to the fields the adapter reads. Real rather than
hand-written because the shape of an overtime final is exactly what is being
parsed, and a hand-written one is a guess about that shape. The POSTPONED and
PENDING cases are built by editing a real event's status block, because no
postponed FBS game appeared on any date sampled -- the branch is still real
code that has to be covered, and the edit is confined to the one field the
branch reads.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import copy  # noqa: E402
import yaml  # noqa: E402

import cfb_grading  # noqa: E402
import signal_report as sr  # noqa: E402
from fetchers import cfb as cfb_fetcher  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfb_games_fixture.json")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


def events():
    with open(FIXTURE) as f:
        return {str(e["id"]): e for e in json.load(f)}


def sides_of(event):
    return cfb_grading._sides(event)


def pick_for(event, side_abbr, bet_type="moneyline"):
    s = sides_of(event)
    return {"bet_type": bet_type, "side": side_abbr,
            "away_abbr": s["away"]["abbr"], "home_abbr": s["home"]["abbr"]}


EV = events()


# ---------------------------------------------------------------- outcomes
# Every final in the fixture, graded both ways: the winner must HIT and the
# loser must MISS. Doing it over all of them rather than one hand-picked game
# means a rule that only works for home favourites cannot pass.
for pk, ev in EV.items():
    if not cfb_grading.is_final(ev):
        continue
    s = sides_of(ev)
    hs, aws = s["home"]["score"], s["away"]["score"]
    if hs == aws:
        continue
    winner = s["home"]["abbr"] if hs > aws else s["away"]["abbr"]
    loser = s["away"]["abbr"] if hs > aws else s["home"]["abbr"]
    text, verdict, basis = cfb_grading.grade(pick_for(ev, winner), ev, False)
    ok("winner HITs ({} {}-{} {})".format(s["away"]["abbr"], aws, hs, s["home"]["abbr"]),
       verdict == "HIT" and basis == "outcome", verdict)
    _t, verdict, _b = cfb_grading.grade(pick_for(ev, loser), ev, False)
    ok("loser MISSes ({} beat {})".format(winner, loser), verdict == "MISS", verdict)

ok("the fixture actually contains finals", sum(
    1 for e in EV.values() if cfb_grading.is_final(e)) >= 8)


# ------------------------------------------------- an unabbreviated program
# A program team_meta has no abbreviation for is carried by its school name,
# which can be two words. The leading-token rule read "Sacramento State" as
# "Sacramento" and recorded the pick UNRESOLVED ("names neither program") --
# 2026-09-12 and 2026-09-19 each lost a pick that way. The event is a real
# final; only the pick's away label is the store's two-word name (Sacramento
# State's, as the store wrote it), since the grade reads labels off the pick.
_fin = next(e for e in EV.values() if cfb_grading.is_final(e)
            and sides_of(e)["home"]["score"] != sides_of(e)["away"]["score"])
_s = sides_of(_fin)
_away_won = _s["away"]["score"] > _s["home"]["score"]
_p = {"bet_type": "moneyline", "side": "Sacramento State",
      "away_abbr": "Sacramento State", "home_abbr": _s["home"]["abbr"]}
_t, _v, _b = cfb_grading.grade(_p, _fin, False)
ok("a two-word school-name side is graded, not UNRESOLVED",
   _v == ("HIT" if _away_won else "MISS") and _b == "outcome", (_t, _v))
_t, _v, _b = cfb_grading.grade(dict(_p, side="Sacramento"), _fin, False)
ok("  while a side naming neither program is still UNRESOLVED", _v == "UNRESOLVED", _v)


# ---------------------------------------------------------------- overtime
ot = [e for e in EV.values()
      if "OT" in ((e["competitions"][0]["status"]["type"].get("detail")) or "")]
ok("the fixture carries a real overtime final", len(ot) >= 1, len(ot))
for e in ot:
    st = e["competitions"][0]["status"]["type"]
    ok("  ESPN keeps name=STATUS_FINAL for it", st.get("name") == "STATUS_FINAL", st.get("name"))
    ok("  is_final() accepts it anyway (reads `completed`, not the name)",
       cfb_grading.is_final(e) is True)
    s = sides_of(e)
    winner = s["home"]["abbr"] if s["home"]["score"] > s["away"]["score"] else s["away"]["abbr"]
    _t, verdict, _b = cfb_grading.grade(pick_for(e, winner), e, False)
    ok("  and it grades as an outcome, not PENDING", verdict == "HIT", verdict)


# --------------------------------------------------------------------- tie
# Not reachable from real data -- which is the point. A tie in the feed means
# the feed is wrong, and the rule must refuse to grade rather than record a
# MISS that reads as a real losing pick.
real = copy.deepcopy(next(e for e in EV.values() if cfb_grading.is_final(e)))
for c in real["competitions"][0]["competitors"]:
    c["score"] = "21"
s = sides_of(real)
text, verdict, basis = cfb_grading.grade(pick_for(real, s["home"]["abbr"]), real, False)
ok("a tie is UNRESOLVED, never MISS", verdict == "UNRESOLVED", verdict)
ok("  and says why in the result text", "tie" in text.lower(), text)
ok("  with no basis (nothing was graded)", basis is None, basis)
text, verdict, _b = cfb_grading.grade(pick_for(real, s["away"]["abbr"]), real, False)
ok("  same for the other side", verdict == "UNRESOLVED", verdict)


# ------------------------------------------------------------- unsettled
sched = copy.deepcopy(next(e for e in EV.values() if cfb_grading.is_final(e)))
sched["competitions"][0]["status"]["type"] = {
    "name": "STATUS_SCHEDULED", "completed": False, "description": "Scheduled",
    "shortDetail": "Sat, 12:00 PM ET"}
s = sides_of(sched)
text, verdict, basis = cfb_grading.grade(pick_for(sched, s["home"]["abbr"]), sched, False)
ok("an unplayed game is PENDING", verdict == "PENDING", verdict)
ok("  carrying ESPN's own status text", text == "Sat, 12:00 PM ET", text)
ok("  is_final() rejects it", cfb_grading.is_final(sched) is False)

for name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED", "STATUS_SUSPENDED"):
    off = copy.deepcopy(sched)
    off["competitions"][0]["status"]["type"] = {
        "name": name, "completed": False, "description": name.split("_")[1].title()}
    s = sides_of(off)
    _t, verdict, _b = cfb_grading.grade(pick_for(off, s["home"]["abbr"]), off, False)
    ok("{} is POSTPONED".format(name), verdict == "POSTPONED", verdict)
    ok("  is_called_off() agrees".format(name), cfb_grading.is_called_off(off) is True)

replayed = copy.deepcopy(sched)
replayed["competitions"][0]["status"]["type"] = {
    "name": "STATUS_POSTPONED", "completed": False, "description": "Postponed"}
s = sides_of(replayed)
pick = pick_for(replayed, s["home"]["abbr"])
pick["replayed_on"] = "2025-12-06"
text, verdict, _b = cfb_grading.grade(pick, replayed, False)
ok("a replayed postponement names the new date", "12-06" in text, text)
ok("  and stays POSTPONED for the original date", verdict == "POSTPONED", verdict)


# ------------------------------------------------------------- malformed
final = next(e for e in EV.values() if cfb_grading.is_final(e))
s = sides_of(final)
_t, verdict, _b = cfb_grading.grade(pick_for(final, "ZZZ"), final, False)
ok("a side naming neither program is UNRESOLVED, not MISS", verdict == "UNRESOLVED", verdict)
_t, verdict, _b = cfb_grading.grade(
    pick_for(final, s["home"]["abbr"], bet_type="spread"), final, False)
ok("a market with no CFB rule is UNRESOLVED, not MISS", verdict == "UNRESOLVED", verdict)
_t, verdict, _b = cfb_grading.grade(pick_for(final, s["home"]["abbr"]), None, False)
ok("a game absent from the slate is UNRESOLVED", verdict == "UNRESOLVED", verdict)
ok("observed_facts() returns None for an ungradeable event",
   cfb_grading.observed_facts(None) is None and cfb_grading.observed_facts(sched) is None)


# ------------------------------------------------------------ the name join
# The property that makes the whole thing work: the abbreviation a pick carries
# is the one _team_ref produces, so the grader has to resolve the event's teams
# the same way rather than trusting ESPN's abbreviation field.
mismatch = 0
for e in EV.values():
    for c in e["competitions"][0]["competitors"]:
        school = c["team"]["location"]
        ours = cfb_fetcher._team_ref(school)["abbr"]
        if ours != c["team"].get("abbreviation"):
            mismatch += 1
        ok("_sides() uses _team_ref for {}".format(school),
           sides_of(e)[c["homeAway"]]["abbr"] == ours)
ok("the two abbreviation vocabularies really do differ (so this matters)",
   mismatch > 0, "{} of {} competitors".format(mismatch, sum(
       len(e["competitions"][0]["competitors"]) for e in EV.values())))


# ------------------------------------------------------- adapter boundary
ok("cfb has an adapter at all", sr.adapter_for("cfb") is not None)
ok("  its grade is cfb_grading's", sr.SPORT_ADAPTERS["cfb"]["grade"] is cfb_grading.grade)
ok("  its store spans dates (7-day fixture window)",
   sr.SPORT_ADAPTERS["cfb"]["store_spans_dates"] is True)
ok("MLB's grade is still MLB's own", sr.SPORT_ADAPTERS["mlb"]["grade"] is sr.grade)
ok("MLB's store still does NOT span dates",
   sr.SPORT_ADAPTERS["mlb"]["store_spans_dates"] is False)
import epl_grading  # noqa: E402
ok("EPL's grade is still EPL's own", sr.SPORT_ADAPTERS["epl"]["grade"] is epl_grading.grade)
ok("an unregistered sport still has no adapter", sr.adapter_for("nhl") is None)


# ---------------------------------------------------- endpoint agreement
with open(os.path.join(REPO, "config.yaml")) as f:
    cfg = yaml.safe_load(f)
ok("config.yaml has a cfb block (without it --sport cfb is refused)",
   isinstance(cfg.get("cfb"), dict))
ok("  its scoreboard_url equals the fetcher's",
   cfg["cfb"]["scoreboard_url"] == cfb_fetcher.ESPN_CFB_SCOREBOARD,
   cfg["cfb"]["scoreboard_url"])
ok("  its fbs_group equals the fetcher's",
   cfg["cfb"]["fbs_group"] == cfb_fetcher.ESPN_FBS_GROUP)
url, group = cfb_grading._scoreboard_url(cfg)
ok("  and the grader reads them", url == cfb_fetcher.ESPN_CFB_SCOREBOARD
   and group == cfb_fetcher.ESPN_FBS_GROUP)
url, group = cfb_grading._scoreboard_url(None)
ok("  falling back to the fetcher's when config is absent",
   url == cfb_fetcher.ESPN_CFB_SCOREBOARD and group == cfb_fetcher.ESPN_FBS_GROUP)


# ---------------------------------------------------------------------------
# THE PAGE LIMIT (2026-09-27). ESPN's college-football scoreboard silently
# returns 25 events for any `limit` over 500 (measured: 500 -> 65 on
# 2026-09-26, 501 -> 25). fetch_slate asked for 1000, so the grader saw 25 of
# 65 games and deferred 12 of 17 picks as "not on this date" -- never graded.
# The stub below answers like ESPN does: every event it has when the limit is
# honoured, the first 25 when it is not. Its events are the fixture's real
# ones, replicated with only `id` changed so there are enough to fill a
# Saturday (the one edit; nothing else in an event is read here).
import espn_dates  # noqa: E402

_real = json.load(open(FIXTURE))


def _saturday(n):
    out = []
    for i in range(n):
        e = copy.deepcopy(_real[i % len(_real)])
        e["id"] = str(900000 + i)
        out.append(e)
    return out


class _ESPN:
    def __init__(self, events):
        self.events, self.params = events, []

    def get(self, url, params=None, timeout=None):
        self.params.append(dict(params or {}))
        lim = int((params or {}).get("limit") or 300)
        page = self.events[:25] if lim > 500 else self.events[:lim]
        body = {"events": page}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return body
        return R()


_cfg = yaml.safe_load(open(os.path.join(REPO, "config.yaml")))
_s = _ESPN(_saturday(65))
_slate = cfb_grading.fetch_slate(_s, _cfg, "2026-09-26")
ok("a 65-game Saturday comes back whole (not ESPN's silent 25)", len(_slate) == 65, len(_slate))
ok("  asking for no more than ESPN honours (<= 500)", int(_s.params[0]["limit"]) <= 500, _s.params[0])
try:
    cfb_grading.fetch_slate(_ESPN(_saturday(espn_dates.SCOREBOARD_LIMIT)), _cfg, "2026-09-26")
    _raised = False
except RuntimeError:
    _raised = True
ok("a page as long as the limit is refused as possibly truncated, not graded from", _raised)
ok("the shared limit is ESPN's largest honoured value", espn_dates.SCOREBOARD_LIMIT == 500)

# THE LIMIT, SWEPT. Every ESPN caller must ask for SCOREBOARD_LIMIT or less; a
# literal over 500 anywhere in them is this bug again.
import re  # noqa: E402
for _f in ("cfb_grading.py", "epl_grading.py", "nfl_grading.py", "espn_odds.py",
           "fetchers/cfb.py", "fetchers/epl.py", "fetchers/nfl.py"):
    _big = [int(m) for m in re.findall(r'["\']limit["\']\s*:\s*(\d+)', open(os.path.join(REPO, _f)).read())
            if int(m) > 500]
    ok("{} asks ESPN for no limit over 500".format(_f), not _big, _big)
ok("fetchers/epl's SCOREBOARD_LIMIT is the shared one",
   __import__("fetchers.epl", fromlist=["x"]).SCOREBOARD_LIMIT == espn_dates.SCOREBOARD_LIMIT)


print("cfb grading: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "cfb grading: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
