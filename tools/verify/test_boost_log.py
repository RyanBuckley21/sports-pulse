"""Regression tests for boost_log: the record of the tickets the Bets tab would
name, and what happened to them.

Run: python3 -m tools.verify.test_boost_log   (from the repo root)

WHY THIS IS PINNED. The log exists to check the page's own promise ("worth
+24.6% per $1") against results. Every way it can go wrong makes that check
look better or worse than the truth, and none of them throws:

  * A TICKET LOGGED AFTER A GAME BEGAN knows something the morning board did
    not. Only legs with a known start still in the future may be on one.
  * THE FIRST CAPTURE OF A DAY WINS. Re-capturing later walks the ticket
    toward the closing price, the same error odds_first_seen was built to end.
  * A LEG SETTLES BY WHICH SIDE IT IS ON. Home and away swapped turns every
    result over.
  * A VOID LEG IS NOT A WIN. A book re-prices the ticket without it; the
    stated chance was for a different ticket, so it is counted apart.
  * ONE TICKET UNDER FIVE PROMOS IS ONE TICKET in the record.
  * THE FILE IS APPEND-ONLY (invariant 6).

THE BOARD is make_fixture's, built by the real bet_board.build from real
2026-09-25 games. Its rows predate `start_utc`, so ONE field is set on them
here (an instant a day after the capture time used). THE RESULTS for the
resolver checks are real: three decided NBA games from
nba_boxscores_fixture.json, read through nba_grading exactly as the grader
reads them. Ticket outcomes in the settle checks are stated in each assertion,
the way test_parlay_boost states prices.

SABOTAGE-CHECKED 2026-11-12 with PYTHONDONTWRITEBYTECODE=1, fourteen cuts, the
suite clean again after each:
  started legs allowed on a ticket             1 fail
  undated legs allowed                         2 fail
  write-once dropped (re-capture each run)     2 fail
  a ticket not worth its stake logged          1 fail
  a side that is neither club kept             1 fail
  home and away swapped at settlement          3 fail
  a void leg counted as a win                  2 fail
  a pending leg settled as won                 1 fail
  settled before the last game's day is over   1 fail
  a ticket settled twice                       1 fail
  the same ticket counted once per promo       1 fail
  reduced tickets counted in the record        1 fail
  the win paid without the boost               2 fail
  the file rewritten instead of appended       1 fail
"""

import copy
import datetime
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import boost_log as bl  # noqa: E402
import nba_grading  # noqa: E402
import parlay_boost as pb  # noqa: E402
import signal_report  # noqa: E402
from fetchers import nba  # noqa: E402
from tools.verify import make_fixture  # noqa: E402

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail)[:300] if detail else ""))


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


NOW = datetime.datetime(2026, 9, 25, 14, 0, tzinfo=datetime.timezone.utc)
LATER = "2026-09-26T23:00:00Z"          # every game a day after the capture
BOARD = make_fixture._bets()
for _row in BOARD["straight"] + BOARD["parlay"]:
    _row["start_utc"] = LATER           # THE ONE EDIT: the fixture predates the field


def sides(row):
    return sorted(l["side"] for l in row["legs"])


def by_key(rows):
    return {(r["promo_id"], r["kind"]): r for r in rows}


# --- capture -----------------------------------------------------------------
ROWS = bl.capture(BOARD, [], now=NOW)
K = by_key(ROWS)
want = pb.suggest([dict(game=l["sport"] + ":" + str(l["id"]), decimal=l["decimal"], fair=l["fair"],
                        label=l["side"]) for l in BOARD["straight"] + BOARD["parlay"]],
                  dict(bl.REFERENCE_PROMOS)["b50-3leg-300"])
v = K.get(("b50-3leg-300", "value"))
ok("capture logs the ticket suggest() names: 3 legs at +300 is TOR CAR CWS, 21.3%, +20.9%",
   v is not None and sides(v) == ["CAR", "CWS", "TOR"] and near(v["win_prob"], want["value"]["win_prob"])
   and near(v["ev_boosted"], want["value"]["ev_boosted"]) and v["boosted_american"] == 468, v)
ok("  a ticket row carries what settling and auditing need",
   v is not None and v["type"] == "ticket" and v["slate_date"] == "2026-09-25"
   and v["captured_at"] == "2026-09-25T14:00:00Z" and v["promo"]["boost_pct"] == 50
   and all(set(l) == {"sport", "id", "side", "side_is_home", "american", "decimal", "fair",
                      "start_utc", "game_date"} for l in v["legs"])
   and all(l["game_date"] == "2026-09-26" for l in v["legs"]), v)
home = {r["side"]: r["side"] == r["home"] for r in BOARD["straight"] + BOARD["parlay"]}
ok("  each leg records which side of its game it is on",
   v is not None and all(l["side_is_home"] == home[l["side"]] for l in v["legs"])
   and len({l["side_is_home"] for r in ROWS for l in r["legs"]}) == 2)
ok("both kinds are logged for every reference promo that has a ticket, each key once",
   len({bl.ticket_key(r) for r in ROWS}) == len(ROWS) and {r["kind"] for r in ROWS} == {"value", "growth"}
   and ("b50-2leg-100", "growth") in K, sorted(K))
ok("a promo no ticket can meet logs nothing (a -200 floor leaves two legs on this board)",
   ("b50-3leg-300-floor200", "value") not in K and ("b50-3leg-300-floor200", "growth") not in K)
ok("WRITE-ONCE: the same day captured again adds nothing", bl.capture(BOARD, ROWS, now=NOW) == [])
moved = copy.deepcopy(BOARD)
for _row in moved["straight"] + moved["parlay"]:
    if _row["side"] == "TOR":
        _row["decimal"], _row["american"] = 1.5, -200
ok("  even when the prices have moved since: the first capture stands",
   bl.capture(moved, ROWS, now=NOW + datetime.timedelta(hours=6)) == [])
ok("  and the next day is a new day",
   len(bl.capture(BOARD, ROWS, now=NOW + datetime.timedelta(hours=20))) == len(ROWS))

started = copy.deepcopy(BOARD)
for _row in started["straight"] + started["parlay"]:
    if _row["side"] == "TOR":
        _row["start_utc"] = "2026-09-25T13:00:00Z"       # an hour before the capture
ok("a game that has started is on no ticket",
   all("TOR" not in sides(r) for r in bl.capture(started, [], now=NOW))
   and any("TOR" in sides(r) for r in ROWS))
undated = copy.deepcopy(BOARD)
for _row in undated["straight"] + undated["parlay"]:
    if _row["side"] == "TOR":
        _row["start_utc"] = None
ok("a leg with no start instant is on no ticket (it cannot be shown to be pre-game)",
   all("TOR" not in sides(r) for r in bl.capture(undated, [], now=NOW)))
blank = copy.deepcopy(BOARD)
for _row in blank["straight"] + blank["parlay"]:
    _row["start_utc"] = None
ok("  a board with no instants at all logs nothing", bl.capture(blank, [], now=NOW) == [])
ok("  and so does an empty or missing board", bl.capture({}, [], now=NOW) == [] and bl.capture(None, [], now=NOW) == [])
odd = copy.deepcopy(BOARD)
for _row in odd["straight"] + odd["parlay"]:
    if _row["side"] == "TOR":
        _row["home"] = _row["away"] = "ELSE"
ok("a row whose side is neither of its clubs is left out, not settled on a guess",
   all("TOR" not in sides(r) for r in bl.capture(odd, [], now=NOW)))
_saved = bl.REFERENCE_PROMOS
bl.REFERENCE_PROMOS = [("b5-3leg", {"boost_pct": 5, "min_legs": 3})]
try:
    ok("a ticket not worth its stake is not logged (a 5% boost cannot beat the vig)",
       bl.capture(BOARD, [], now=NOW) == [])
finally:
    bl.REFERENCE_PROMOS = _saved
ok("the reference promo ids are unique, and every one is a 50% boost with a 2+ leg minimum",
   len({i for i, _ in bl.REFERENCE_PROMOS}) == len(bl.REFERENCE_PROMOS) == 5
   and all(p["boost_pct"] == 50 and p["min_legs"] >= 2 for _, p in bl.REFERENCE_PROMOS))
JS = open(os.path.join(_ROOT, "web", "insights", "bets.js")).read()
ok("they are a measurement grid, not a default: the page's promo still starts blank",
   'var promo = { boost: "", legs: "", total: "", floor: "" };' in JS and "b50-3leg" not in JS)

# --- one leg, from real results ----------------------------------------------
with open(os.path.join(_ROOT, "tools", "verify", "nba_boxscores_fixture.json")) as fh:
    EVENTS = json.load(fh)["events"]
DAY = "2026-11-10"
SLATE = nba_grading.slate_from_events(EVENTS, DAY)
home_win = next(e for e in SLATE.values() if nba_grading.observed_facts(e)["result"] == "H")
away_win = next(e for e in SLATE.values() if nba_grading.observed_facts(e)["result"] == "A")


def leg(event, is_home, sport="nba", day=DAY):
    return {"sport": sport, "id": str(event["id"]), "side": "X", "side_is_home": is_home, "game_date": day}


ok("a real home win: the home leg won and the away leg lost",
   bl.leg_result(leg(home_win, True), nba_grading.observed_facts(home_win), False) == "won"
   and bl.leg_result(leg(home_win, False), nba_grading.observed_facts(home_win), False) == "lost")
ok("a real away win: the other way round",
   bl.leg_result(leg(away_win, False), nba_grading.observed_facts(away_win), False) == "won"
   and bl.leg_result(leg(away_win, True), nba_grading.observed_facts(away_win), False) == "lost")
ok("a tie or a called-off game voids the leg; an undecided one is not settled",
   bl.leg_result(leg(home_win, True), {"result": "T"}, False) == "void"
   and bl.leg_result(leg(home_win, True), None, True) == "void"
   and bl.leg_result(leg(home_win, True), None, False) is None
   and bl.leg_result(leg(home_win, True), {}, False) is None)

calls = []
_real = signal_report.SPORT_ADAPTERS["nba"]["fetch_slate"]
signal_report.SPORT_ADAPTERS["nba"]["fetch_slate"] = (
    lambda session, config, date: calls.append(date) or nba_grading.slate_from_events(EVENTS, date))
try:
    resolve = bl.adapter_resolver(None, {})
    got = [resolve(leg(home_win, True)), resolve(leg(away_win, True)), resolve(leg(away_win, False)),
           resolve(dict(leg(home_win, True), id="0")), resolve(leg(home_win, True, sport="curling"))]
finally:
    signal_report.SPORT_ADAPTERS["nba"]["fetch_slate"] = _real
ok("the resolver settles legs through the grader's own adapter",
   got == ["won", "lost", "won", None, None], got)
ok("  and reads each sport-day's slate once, however many legs are on it", calls == [DAY], calls)

# --- a ticket ------------------------------------------------------------------
T = K[("b50-3leg-300", "value")]
ORDER = [l["side"] for l in T["legs"]]


def settle_with(outcomes, rows=None, today="2026-09-27", seen=None):
    """Settle with stated per-side outcomes; `seen` collects the legs asked about."""
    def resolve(l):
        if seen is not None:
            seen.append(l["side"])
        return outcomes.get(l["side"])
    return bl.settle(rows if rows is not None else [T], resolve, today=today, now=NOW)


won = settle_with({s: "won" for s in ORDER})
ok("every leg won: the ticket won, paid at ITS boost ((dec-1) x 1.5 per $1)",
   len(won) == 1 and won[0]["outcome"] == "won" and won[0]["key"] == list(bl.ticket_key(T))
   and near(won[0]["return_per_dollar"], (T["decimal"] - 1.0) * 1.5)
   and round(won[0]["return_per_dollar"], 2) == 4.68, won)
ok("  the result row names each leg's result",
   [(l["id"], l["result"]) for l in won[0]["legs"]] == [(l["id"], "won") for l in T["legs"]])
lost = settle_with({ORDER[0]: "won", ORDER[1]: "lost", ORDER[2]: "won"})
ok("one leg lost: the ticket lost its stake", lost[0]["outcome"] == "lost" and lost[0]["return_per_dollar"] == -1.0)
early = settle_with({ORDER[0]: "lost"})
ok("  a loss settles it even while other legs are undecided: nothing can undo it",
   len(early) == 1 and early[0]["outcome"] == "lost")
ok("no loss and one leg undecided: not settled, left for the next run",
   settle_with({ORDER[0]: "won", ORDER[1]: "won"}) == [])
void = settle_with({ORDER[0]: "won", ORDER[1]: "void", ORDER[2]: "won"})
ok("a void leg: 'reduced', with no return claimed -- not a win at the stated price",
   void[0]["outcome"] == "reduced" and void[0]["return_per_dollar"] is None)
asked = []
ok("not looked at until its last game's day is over: no result, and no slate requested",
   settle_with({s: "won" for s in ORDER}, today="2026-09-26", seen=asked) == [] and asked == [])
ok("a settled ticket is never settled again", settle_with({s: "won" for s in ORDER}, rows=[T] + won) == [])

# --- the record ----------------------------------------------------------------
ok("nothing settled: every figure is None, nothing is invented",
   bl.summary(ROWS)["settled"] == 0 and bl.summary(ROWS)["cash_rate"] is None
   and bl.summary([])["logged"] == 0 and "none settled yet" in bl.report(ROWS))
same = [r for r in ROWS if sides(r) == sides(T)]
ok("the fixture holds the case: the same three legs are logged under several promos and kinds",
   len(same) >= 3 and len({bl.ticket_key(r) for r in same}) == len(same), [bl.ticket_key(r) for r in same])
all_won = [x for r in ROWS for x in bl.settle([r], lambda l: "won", today="2026-09-27", now=NOW)]
distinct = {(r["slate_date"], tuple(sides(r))) for r in ROWS}
s_all = bl.summary(ROWS + all_won)
ok("ONE TICKET IS COUNTED ONCE: {} rows, {} distinct tickets, {} in the record".format(
    len(ROWS), len(distinct), s_all["settled"]),
   s_all["settled"] == s_all["logged"] == len(distinct) < len(ROWS), s_all)
two = [T, K[("b50-2leg-100", "growth")]]
res = (bl.settle([two[0]], lambda l: "won", today="2026-09-27", now=NOW)
       + bl.settle([two[1]], lambda l: "lost", today="2026-09-27", now=NOW))
s2 = bl.summary(two + res)
ok("one won, one lost: cash rate 50% against the mean stated chance, gap = the difference",
   s2["settled"] == 2 and s2["cashed"] == 1 and s2["cash_rate"] == 0.5
   and near(s2["expected_rate"], (two[0]["win_prob"] + two[1]["win_prob"]) / 2)
   and near(s2["rate_gap"], 0.5 - s2["expected_rate"]) and len(s2["rate_gap_ci"]) == 2, s2)
ok("  return is the mean of +4.68 and -1.00 per $1, against the mean stated value",
   near(s2["roi"], ((two[0]["decimal"] - 1) * 1.5 - 1.0) / 2)
   and near(s2["expected_roi"], (two[0]["ev_boosted"] + two[1]["ev_boosted"]) / 2)
   and near(s2["roi_gap"], s2["roi"] - s2["expected_roi"]), s2)
red = bl.settle([two[1]], lambda l: "void", today="2026-09-27", now=NOW)
s3 = bl.summary(two + res[:1] + red)
ok("a reduced ticket is counted apart and is in neither comparison",
   s3["reduced"] == 1 and s3["settled"] == 1 and s3["cash_rate"] == 1.0, s3)
ok("the per-kind record reads only that kind",
   bl.summary(two + res, "value")["settled"] == 1 and bl.summary(two + res, "growth")["cashed"] == 0)
ok("the report says what it cannot claim",
   "counted once" in bl.report(two + res) and "too narrow" in bl.report(two + res))

# --- the file ------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "data", "boost_tickets.jsonl")
    ok("a log that does not exist yet reads as empty", bl.load(path) == [])
    bl.append(ROWS[:2], path)
    first = open(path).read()
    bl.append(won, path)
    bl.append([], path)
    after = open(path).read()
    ok("APPEND-ONLY: what was written is still there, byte for byte, with the new row after it",
       after.startswith(first) and len(after) > len(first) and bl.load(path) == ROWS[:2] + won)
ok("the committed log is never written by this suite", bl.LOG_PATH.endswith(os.path.join("data", "boost_tickets.jsonl")))

print("boost log: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "boost log: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
