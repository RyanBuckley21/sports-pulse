"""Regression tests for the season game log behind the prop check
(fetchers.nfl.aggregate_category's `log`, carried by generate_stats).

Run: python3 -m tools.verify.test_prop_log   (from the repo root)

WHY THIS IS PINNED. The player page counts a season against a line the reader
types ("over 62.5 in 6 of 9"). The count is only as good as the list it
counts, and each way the list can be wrong still prints a tidy fraction:

  * THE WHOLE SEASON, NOT THE WINDOW. The board ranks on the last four games;
    the log must hold every game played. Cut to the window it answers "how
    often" from four games and says so with a straight face.
  * ONE GAME'S NUMBER, NOT AN AVERAGE. A prop line is a single game's total,
    so each entry is that game's raw value even on a per-game board.
  * EVERY FIELD OF THE CATEGORY. Total TDs is rushing plus receiving; a log
    that kept the first field would undercount a back's receiving scores.
  * OLDEST FIRST, WITH ITS DATE, and the board's own last-four `series` is
    exactly the log's tail -- the two must describe the same games.
  * THE SAME PLAYERS AS THE BOARD. A position filter that drops a running back
    from the receiving board drops his log with him.

Fixture: tools/verify/nfl_player_weeks_fixture.json -- REAL 2026 player-week
rows for seven players (the top of three boards on 2026-11-11), trimmed to the
fields the aggregation reads; provenance in the file. One edge edits one
field: a row whose game_id is blanked, for the "no date known" branch.

Sabotage-checked when written (2026-11-11, PYTHONDONTWRITEBYTECODE=1, source
byte-identical afterwards), failures measured, of 14:
  L1 log cut to the board's window               9 fail
  L2 log keeps only the category's first field   2 fail
  L3 log newest-first                            6 fail
  L4 log holds the board's average, not the game 8 fail
  L5 log drops scoreless games                   2 fail
  L6 log loses its dates                         3 fail
L6 first CRASHED the suite (sorting a list of None); the date checks now test
for missing dates before ordering them.
"""

import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from fetchers import nfl  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail)[:300] if detail else ""))


FX = json.load(open(os.path.join(HERE, "nfl_player_weeks_fixture.json")))
CONFIG = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))
CATS = {c["key"]: c for c in CONFIG["nfl"]["stat_categories"]}
WINDOW = CONFIG["nfl"].get("window_games", 4)
DATES = FX["gameday_by_game_id"]


def board(key, rows=None):
    by_player = nfl._rows_by_player(rows if rows is not None else FX["rows"], 2026)
    recs = nfl.aggregate_category(by_player, CATS[key], WINDOW, DATES)
    return {r["entity"]: r for r in recs}


def values(rec):
    return [g["value"] for g in (rec or {}).get("log") or []]


REC = board("receiving_yards")
jsn = REC.get("Jaxon Smith-Njigba") or {}
ok("the log is the WHOLE season: nine games, not the board's four",
   values(jsn) == [122.0, 155.0, 128.0, 76.0, 102.0, 134.0, 150.0, 118.0, 51.0], values(jsn))
ok("  while the board still ranks on the last four (113.25 a game)",
   jsn.get("value") == 113.25 and len(jsn.get("series") or []) == 4, (jsn.get("value"), jsn.get("series")))
ok("  each entry is ONE game's total, not the per-game average the board shows",
   113.25 not in values(jsn) and max(values(jsn) or [0]) == 155.0)
ok("  and the board's series is exactly the log's last four games",
   [(g["date"], g["value"]) for g in jsn.get("series") or []]
   == [(g["date"], g["value"]) for g in (jsn.get("log") or [])[-4:]], jsn.get("series"))
dates = [g["date"] for g in jsn.get("log") or []]
# all(dates) first: sorting a list holding None raises (L6 first crashed here).
ok("  oldest first, every game with its date",
   all(dates) and dates == sorted(dates) and len(set(dates)) == 9, dates)
ok("  the dates are the schedule's, joined on game_id",
   bool(dates) and all(dates) and set(dates) <= set(DATES.values()))
ok("a player with a shorter season has a shorter log (8 games, not padded to 9)",
   len(values(REC.get("Malik Nabers"))) == 8 and len(values(REC.get("Ja'Marr Chase"))) == 8)

TD = board("total_tds")
ok("Total TDs sums BOTH of its fields game by game (McCaffrey: 10 in 8 games)",
   values(TD.get("Christian McCaffrey")) == [0.0, 2.0, 1.0, 1.0, 1.0, 2.0, 1.0, 2.0],
   values(TD.get("Christian McCaffrey")))
rows_cmc = [r for r in FX["rows"] if r.get("player_display_name") == "Christian McCaffrey"]
ok("  and that really is two fields: his receiving TDs alone are fewer",
   sum(float(r.get("receiving_tds") or 0) for r in rows_cmc) > 0
   and sum(float(r.get("rushing_tds") or 0) for r in rows_cmc) > 0
   and sum(values(TD.get("Christian McCaffrey"))) == sum(
       float(r.get("receiving_tds") or 0) + float(r.get("rushing_tds") or 0) for r in rows_cmc))
ok("a scoreless game is a 0 in the log, not a missing game (Swift: three zeros in a row)",
   values(TD.get("D'Andre Swift"))[:4] == [3.0, 0.0, 0.0, 0.0], values(TD.get("D'Andre Swift")))

ok("the position filter drops a running back from the receiving board, log and all",
   "Christian McCaffrey" not in REC and "Christian McCaffrey" in TD)
PASSING = board("passing_yards")
ok("a quarterback's log is on the passing board (Burrow: 8 games, 462 the high)",
   len(values(PASSING.get("Joe Burrow"))) == 8 and max(values(PASSING.get("Joe Burrow")) or [0]) == 462.0)

# EDGE, one field edited: the schedule has no date for a game (game_id blank).
edited = copy.deepcopy(FX["rows"])
target = next(r for r in edited if r.get("player_display_name") == "Jaxon Smith-Njigba" and r.get("week") == "1")
target["game_id"] = ""
log = (board("receiving_yards", edited).get("Jaxon Smith-Njigba") or {}).get("log") or []
ok("a game with no date known keeps its value and carries date None",
   len(log) == 9 and log[0]["date"] is None and log[0]["value"] == 122.0, log[:1])

GS = open(os.path.join(_ROOT, "generate_stats.py")).read()
ok("generate_stats carries the log onto the page's player rows", '"log": r.get("log"),' in GS)

print("prop log: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "prop log: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
