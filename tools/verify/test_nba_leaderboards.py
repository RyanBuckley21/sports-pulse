"""Regression tests for NBA's Who's Hot boards: fetchers.nba.fetch and the
box-score reading under it.

Run: python3 -m tools.verify.test_nba_leaderboards   (from the repo root)

WHY THIS IS PINNED. The boards went live on 2026-11-11, built in a sandbox that
cannot reach ESPN, on box scores captured from a GitHub runner. Every failure
here is silent -- a board still renders ten names with plausible numbers:

  * A NIGHT OFF IS NOT A GAME WITH ZEROS. 457 of the fixture's 3,108 athlete
    rows are did-not-plays. Counted as appearances they would drag a resting
    star's average down and push his real games out of his window.
  * STATS ARE READ BY KEY. ESPN sends a list of values beside a list of keys;
    read by position, a reordered feed swaps points for rebounds and nothing
    throws.
  * THE DAY IS EASTERN (invariant 3). 94 of the 117 games carry the next UTC
    date. Filed by UTC, a back-to-back reads as two games on one day and the
    window's edges move by a day.
  * NO LOOK-AHEAD, AND NO GAME THAT DOES NOT COUNT. Preseason and postponed
    shells sit in the same feed.
  * ALL OR NOTHING (invariant 5). One failed box score raises; four games
    published as "last 5" would be wrong without looking wrong.
  * THE WINDOW ENDS ON THE LAST DAY THE LEAGUE PLAYED, so the boards survive
    the All-Star break and the end of the regular season instead of emptying
    a day at a time.

THE FIXTURE is nba_boxscores_fixture.json: 117 decided games, 2026-10-26 to
2026-11-10, captured 2026-11-12T00:13Z by tools/capture_nba_boxscores.py on a
GitHub runner (run 43125639054) and trimmed to the fields read. Nothing in it
is hand-written. The cases come from it as played:

  Luka Doncic      five games, 45/42/40/53/32 points: the worked example
  Pascal Siakam    sat out 11-02 between games he played
  Nique Clifford   six games for SAC, then one for OKC on 11-10
  Ja Morant        two games in the window (under the floor)
  Ivica Zubac      three games (at the floor)
  (no one)         a 28-minute UTAH line with no athlete id, name or position

Three edges were never observed and are made by editing ONE field of a real
row, each in a copy: a preseason game (season.type 2 -> 1), a postponed shell
(status.type.completed true -> false) and a stat the feed left blank
(a points value -> "--").

SABOTAGE-CHECKED 2026-11-11 with PYTHONDONTWRITEBYTECODE=1, sixteen cuts, each
restored and the suite re-run clean. What each one failed:

  did-not-plays kept as zero lines          -> the played-row count, the
                                               independent reading, Siakam's
                                               window, Zubac's three games
  stats read by position (keys ignored)     -> the reordered-line check (and
                                               the blank-stat parse check)
  paired "3-6" not split                    -> "the captured feed builds at
                                               all": the fetcher itself raises
                                               on the missing made-threes
                                               field, which is the guard
  a blank stat read as 0                    -> both blank-stat checks
  a missing field averaged around           -> the blank-stat raise
  day taken from the UTC stamp              -> the Eastern-date checks, the
                                               series dates, the request
                                               counts, the anchor checks
  window = first N games, not last N        -> Doncic, the independent reading,
                                               Siakam, Clifford's club
  the min_games floor dropped               -> Morant's absence, the
                                               floor-to-window bounds
  per-game value not divided                -> every value check
  counted-game rule skipped                 -> the preseason checks
  undecided games requested                 -> the postponed check alone
  a failed summary skipped                  -> the all-or-nothing check alone
  window anchored to today, not last played -> both anchor checks, the request
                                               counts
  `played` not bounded by today             -> "before the first game" alone.
                                               The 11-05 run still passes:
                                               the scoreboard walk is bounded
                                               by today too, so later games
                                               never arrive -- two guards
  config not validated on an empty day      -> the empty-day mode check alone
  SPORT_FETCHERS["nba"] pointed elsewhere   -> the registration check alone

MINUTES (2026-11-12, 9 checks). Each row also carries `usage` -- minutes a
game over the window, the stat per 36 minutes, and minutes in the games just
before the window -- and each series entry its `min`. Sabotaged six ways:
  per 36 replaced by the per-game value        5 fail
  "before" taken over the window itself        5 fail
  one earlier game treated as a baseline       2 fail
  minutes averaged over the whole lookback     5 fail
  per-game minutes dropped from the series     3 fail
  a box score with no minutes tolerated        1 fail
"""

import contextlib
import copy
import datetime
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402

import generate_stats  # noqa: E402
import normalizer  # noqa: E402
import team_meta  # noqa: E402
from fetchers import nba  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail)[:300] if detail else ""))


def raises(fn, *args, **kwargs):
    """The exception fn raised, or None. Its log lines are swallowed."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn(*args, **kwargs)
    except Exception as exc:        # noqa: BLE001 -- the check is that it raised
        return exc
    return None


with open(os.path.join(_ROOT, "config.yaml")) as fh:
    CONFIG = yaml.safe_load(fh)
NBA_CFG = CONFIG["nba"]
with open(os.path.join(HERE, "nba_boxscores_fixture.json")) as fh:
    FX = json.load(fh)
EVENTS, SUMMARIES = FX["events"], FX["summaries"]


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError("HTTP {}".format(self.status))

    def json(self):
        return self.payload


class _Session:
    """Serves the captured feed: a scoreboard month by its `dates` key, a
    summary by event id. `fail` is an event id whose summary answers 500."""

    def __init__(self, events=EVENTS, summaries=SUMMARIES, fail=None):
        self.events, self.summaries, self.fail = events, summaries, fail
        self.scoreboard, self.summary = [], []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        if url == NBA_CFG["summary_url"]:
            self.summary.append(params["event"])
            if params["event"] == self.fail:
                return _Resp({}, 500)
            return _Resp(self.summaries[params["event"]])
        self.scoreboard.append(params)
        month = str(params["dates"])[:6]
        return _Resp({"events": [e for e in self.events
                                 if (e.get("date") or "").replace("-", "")[:6] == month]})


def run(today="2026-11-11", config=CONFIG, **kw):
    sess = _Session(**kw)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        records = nba.fetch(config, today=today, session=sess)
    return records, sess, buf.getvalue()


def board(records, key):
    rows = [r for r in records if r["stat_category"] == key]
    rows.sort(key=lambda r: -r["value"])
    return rows


def row_for(records, key, name):
    hits = [r for r in records if r["stat_category"] == key and r["entity"] == name]
    return hits[0] if len(hits) == 1 else None


def edited(event_id, fn):
    """The captured events with ONE of them changed by fn, in a deep copy."""
    out = copy.deepcopy(EVENTS)
    for e in out:
        if str(e["id"]) == str(event_id):
            fn(e)
    return out


# --- an independent reading of the fixture, sharing no code with the fetcher --
def raw_lines():
    """{player name: [(eastern day, team, played?, {key: raw string})]} read
    straight from the fixture, oldest first."""
    eastern = {}
    for e in EVENTS:
        when = datetime.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        # Every game in the fixture falls in EST or EDT; -5h after 11-01 06:00Z.
        shift = 4 if when < datetime.datetime(2026, 11, 1, 6, tzinfo=datetime.timezone.utc) else 5
        eastern[str(e["id"])] = (when - datetime.timedelta(hours=shift)).date().isoformat()
    out = {}
    for gid, summary in SUMMARIES.items():
        for block in summary["boxscore"]["players"]:
            for group in block["statistics"]:
                for a in group["athletes"]:
                    out.setdefault(a["athlete"]["displayName"], []).append((
                        eastern[gid], block["team"]["abbreviation"], not a["didNotPlay"],
                        dict(zip(group["keys"], a["stats"]))))
    for lines in out.values():
        lines.sort(key=lambda l: l[0])
    return out


RAW = raw_lines()
try:
    RECORDS, SESS, LOG = run()
except Exception as exc:            # noqa: BLE001 -- reported as a failure, not a traceback
    print("nba leaderboards: 0 PASS, 1 FAIL")
    print("  FAIL the captured feed builds at all: {}: {}".format(type(exc).__name__, exc))
    sys.exit(1)

# --- the box score ---------------------------------------------------------
keys = SUMMARIES[next(iter(SUMMARIES))]["boxscore"]["players"][0]["statistics"][0]["keys"]
line = nba.parse_stat_line(keys, ["36", "29", "11-22", "3-6", "4-4", "10", "0", "0", "1", "0", "0", "10", "2", "+6"])
ok("parse_stat_line reads a real line by key",
   line.get("points") == 29 and line.get("rebounds") == 10 and line.get("minutes") == 36
   and line.get("plusMinus") == 6, line)
ok("  a paired split yields both halves under plain names",
   line.get("threePointFieldGoalsMade") == 3 and line.get("threePointFieldGoalsAttempted") == 6
   and line.get("fieldGoalsMade") == 11 and line.get("freeThrowsAttempted") == 4, line)
swapped = nba.parse_stat_line(["rebounds", "points"], ["10", "29"])
ok("  keys decide, not position: a reordered line reads the same",
   swapped == {"rebounds": 10, "points": 29}, swapped)
blank = nba.parse_stat_line(["points", "rebounds"], ["--", "4"])
ok("  a blank stat is left out, never read as 0", blank == {"rebounds": 4}, blank)
ok("  a line that cannot be aligned to its keys raises",
   isinstance(raises(nba.parse_stat_line, ["points", "rebounds"], ["4"]), ValueError))

played = sum(1 for lines in RAW.values() for l in lines if l[2])
sat = sum(1 for lines in RAW.values() for l in lines if not l[2])
got = sum(len(nba.extract_player_rows(s)) for s in SUMMARIES.values())
nobody = [(gid, block["team"]["abbreviation"]) for gid, summary in SUMMARIES.items()
          for block in summary["boxscore"]["players"] for group in block["statistics"]
          for a in group["athletes"] if not a["didNotPlay"] and not a["athlete"]["id"]]
ok("extract_player_rows keeps the 2,650 it can name, of 2,651 who played, and none of the 457 who did not",
   (played, sat, got) == (2651, 457, 2650), (played, sat, got))
ok("  the one it drops is a real line ESPN attached to no athlete (UTAH, 401909890)",
   nobody == [("401909890", "UTAH")], nobody)
first = nba.extract_player_rows(SUMMARIES[next(iter(SUMMARIES))])
ok("  each row names its club by an abbreviation team_meta knows",
   all(r["team"] in team_meta.NBA_TEAMS and r["position"] in ("G", "F", "C") and r["name"]
       for r in first), first[:1])
ok("  a summary with no player block raises (never 'nobody played')",
   isinstance(raises(nba.extract_player_rows, {"boxscore": {}}), ValueError)
   and isinstance(raises(nba.extract_player_rows, {}), ValueError))

# --- the worked example, against the independent reading ---------------------
luka = row_for(RECORDS, "points_pg", "Luka Doncic")
raw_luka = [l for l in RAW["Luka Doncic"] if l[2]][-5:]
ok("Doncic: 42.4 points a game over his last five, as the fixture holds them",
   luka is not None and luka["value"] == 42.4
   and [s["value"] for s in luka["series"]] == [45, 42, 40, 53, 32]
   == [int(l[3]["points"]) for l in raw_luka], luka)
ok("  the series is dated by the games he played, oldest first, and the row by the last",
   luka is not None and [s["date"] for s in luka["series"]] == [l[0] for l in raw_luka]
   and luka["last_game_date"] == "2026-11-09" and luka["games_window"] == 5
   and luka["team"] == "LAL" and luka["position"] == "G"
   and luka["window"] == "last_5_games_per_game", luka)
pra = row_for(RECORDS, "pra_pg", "Luka Doncic")
ok("  his combined board is the sum of the three fields, game by game",
   pra is not None and [s["value"] for s in pra["series"]] ==
   [int(l[3]["points"]) + int(l[3]["rebounds"]) + int(l[3]["assists"]) for l in raw_luka]
   and pra["value"] == 59.4, pra)
threes = row_for(RECORDS, "threes_pg", "Luka Doncic")
ok("  his threes are the MADE half of the paired split",
   threes is not None and [s["value"] for s in threes["series"]] ==
   [int(l[3]["threePointFieldGoalsMade-threePointFieldGoalsAttempted"].split("-")[0])
    for l in raw_luka] and threes["value"] == 5.6, threes)

mismatch = []
for cat in NBA_CFG["stat_categories"]:
    for r in board(RECORDS, cat["key"])[:10]:
        last = [l for l in RAW[r["entity"]] if l[2]][-5:]
        want = []
        for l in last:
            total = 0
            for f in cat["fields"]:
                if f == "threePointFieldGoalsMade":
                    total += int(l[3]["threePointFieldGoalsMade-threePointFieldGoalsAttempted"].split("-")[0])
                else:
                    total += int(l[3][f])
            want.append(total)
        if [s["value"] for s in r["series"]] != want or r["value"] != round(sum(want) / len(want), 1):
            mismatch.append((cat["key"], r["entity"], r["value"], want))
ok("every board's top ten matches the independent reading, value and series",
   not mismatch, mismatch[:3])

# --- a night off is not a zero ---------------------------------------------
siakam_raw = RAW["Pascal Siakam"]
ok("the fixture holds the case: Siakam sat out 11-02 between games he played",
   [(l[0], l[2]) for l in siakam_raw][-5:] ==
   [("2026-11-01", True), ("2026-11-02", False), ("2026-11-04", True),
    ("2026-11-07", True), ("2026-11-09", True)], siakam_raw[-5:])
siakam = row_for(RECORDS, "points_pg", "Pascal Siakam")
ok("  his window skips the night off and reaches back to 10-30 instead",
   siakam is not None and [s["date"] for s in siakam["series"]] ==
   ["2026-10-30", "2026-11-01", "2026-11-04", "2026-11-07", "2026-11-09"]
   and [s["value"] for s in siakam["series"]] == [13, 17, 20, 23, 29]
   and siakam["value"] == 20.4, siakam)

# --- minutes beside the average (2026-11-12) ---------------------------------
# Re-derived from the raw lines: minutes over the window, the stat per 36 of
# them (TOTAL over TOTAL), and minutes in the games just before the window.
def usage_by_hand(name, field):
    played = [l for l in RAW[name] if l[2]]
    win, before = played[-5:], played[:-5]
    mins = [int(l[3]["minutes"]) for l in win]
    total = sum(int(l[3][field]) for l in win)
    prev = [int(l[3]["minutes"]) for l in before]
    return {"minutes": round(sum(mins) / len(mins), 1), "per36": round(36.0 * total / sum(mins), 1),
            "prev_minutes": round(sum(prev) / len(prev), 1) if len(prev) >= 2 else None,
            "prev_games": len(prev)}


ok("Doncic: 36.6 minutes a game, 41.7 points per 36, and 37.0 in his two games before",
   luka is not None and luka.get("usage") == usage_by_hand("Luka Doncic", "points")
   == {"minutes": 36.6, "per36": 41.7, "prev_minutes": 37.0, "prev_games": 2}, luka and luka.get("usage"))
wemby = row_for(RECORDS, "points_pg", "Victor Wembanyama")
ok("a role that shrank is two numbers: Wembanyama 26.6 minutes now, 33.3 in the three games before",
   wemby is not None and wemby["usage"]["minutes"] == 26.6 and wemby["usage"]["prev_minutes"] == 33.3
   and wemby["usage"]["prev_games"] == 3 and wemby["usage"] == usage_by_hand("Victor Wembanyama", "points"),
   wemby and wemby.get("usage"))
ok("  per 36 is total over total (44.1), not the average of each game's rate",
   wemby is not None and wemby["usage"]["per36"] == 44.1
   and round(sum(36.0 * s["value"] / s.get("min", float("nan")) for s in wemby["series"]) / 5, 1) != 44.1, wemby and wemby["series"])
ok("  each game in the series carries its own minutes",
   luka is not None and [s.get("min") for s in luka["series"]] ==
   [int(l[3]["minutes"]) for l in raw_luka], luka and luka["series"])
bad_usage = []
for cat in NBA_CFG["stat_categories"]:
    if len(cat["fields"]) != 1 or cat["fields"][0] == "threePointFieldGoalsMade":
        continue
    for r in board(RECORDS, cat["key"])[:10]:
        if r.get("usage") != usage_by_hand(r["entity"], cat["fields"][0]):
            bad_usage.append((cat["key"], r["entity"], r.get("usage")))
ok("every top-ten row's minutes, per-36 and earlier minutes match the independent reading",
   not bad_usage, bad_usage[:3])
ok("  the same player's minutes agree on every board he is on",
   len({json.dumps(r["usage"]["minutes"]) for r in RECORDS if r["entity"] == "Luka Doncic"}) == 1)
ok("one earlier game is not a baseline: with fewer than two, no earlier minutes are stated",
   all(r["usage"]["prev_minutes"] is None for r in RECORDS if r["usage"]["prev_games"] < 2)
   and any(r["usage"]["prev_games"] == 1 for r in RECORDS)
   and all(r["usage"]["prev_minutes"] is not None for r in RECORDS if r["usage"]["prev_games"] >= 2))
no_min = copy.deepcopy(SUMMARIES)
a_game = next(str(e["id"]) for e in EVENTS if nba.slate_date(e) == "2026-11-10")
blk = no_min[a_game]["boxscore"]["players"][0]["statistics"][0]
scorer = max((a for a in blk["athletes"] if a["stats"]), key=lambda a: int(a["stats"][1]))
scorer["stats"][blk["keys"].index("minutes")] = "--"
exc_min = raises(nba.fetch, CONFIG, today="2026-11-11", session=_Session(summaries=no_min))
ok("a box score with no minutes raises, naming the player",
   isinstance(exc_min, ValueError) and "minutes" in str(exc_min)
   and scorer["athlete"]["displayName"] in str(exc_min), exc_min)

# --- the window and its floor ------------------------------------------------
clifford = row_for(RECORDS, "points_pg", "Nique Clifford")
ok("a traded player keeps one window and shows his CURRENT club",
   clifford is not None and clifford["team"] == "OKC" and clifford["games_window"] == 5
   and clifford["series"][-1] == {"date": "2026-11-10", "value": 4, "min": 20}
   and [l[1] for l in RAW["Nique Clifford"] if l[2]][-5:] == ["SAC"] * 4 + ["OKC"], clifford)
ok("under the floor: Morant's two games (18.5 a night) rank on no board",
   len([l for l in RAW["Ja Morant"] if l[2]]) == 2
   and not [r for r in RECORDS if r["entity"] == "Ja Morant"])
zubac = row_for(RECORDS, "rebounds_pg", "Ivica Zubac")
ok("at the floor: Zubac's three games rank, and say three",
   zubac is not None and zubac["games_window"] == 3 and len(zubac["series"]) == 3, zubac)
ok("every category in config is a per-game board with the floor",
   all(c.get("per_game") is True and c.get("min_games") == 3 and c.get("mode") == "rolling_sum"
       for c in NBA_CFG["stat_categories"]))
ok("no row is built on fewer games than the floor or more than the window",
   all(3 <= r["games_window"] <= 5 == NBA_CFG["window_games"] for r in RECORDS))

# --- Eastern dates -----------------------------------------------------------
late = [e for e in EVENTS if nba.slate_date(e) != e["date"][:10]]
ok("the fixture holds the case: 94 of 117 games carry the next UTC date",
   (len(late), len(EVENTS)) == (94, 117), (len(late), len(EVENTS)))
utc_days = {e["date"][:10] for e in EVENTS}
ok("  no row is dated 2026-11-11, the UTC date of the last night's games",
   "2026-11-11" in utc_days and all(s["date"] <= "2026-11-10" for r in RECORDS for s in r["series"]))
ok("  no player has two games on one day",
   all(len({s["date"] for s in r["series"]}) == len(r["series"]) for r in RECORDS))

# --- which games, and how many requests --------------------------------------
ok("one summary request per game in the window, each asked once",
   len(SESS.summary) == 117 == len(set(SESS.summary)), len(SESS.summary))
ok("  the scoreboard is walked by month key under the limit, never a range",
   SESS.scoreboard and all(len(str(p["dates"])) == 6 and p.get("limit") == 500
                           for p in SESS.scoreboard), SESS.scoreboard)
ok("  the log line states what was read",
   "117 games 2026-10-26..2026-11-10" in LOG and "depth 5 of 5" in LOG
   and "last played" not in LOG, LOG)
by_club = {}
for lines in RAW.values():
    for day, club, did_play, _ in lines:
        by_club.setdefault(club, set()).add(day)
counts = sorted(len(days) for days in by_club.values())
ok("the 16-day lookback holds 6 to 9 games for every one of the 30 clubs",
   len(counts) == 30 and counts[0] == 6 and counts[-1] == 9
   and NBA_CFG["lookback_days"] == 16 == nba.LEADERBOARD_LOOKBACK_DAYS, counts)

LAST_NIGHT = [str(e["id"]) for e in EVENTS if nba.slate_date(e) == "2026-11-10"]


def to_preseason(e):
    e["season"]["type"] = 1


def to_postponed(e):
    e["competitions"][0]["status"]["type"]["completed"] = False


pre, pre_sess, _ = run(events=edited(LAST_NIGHT[0], to_preseason))
ok("a preseason game is not read: no request for it, 116 instead of 117",
   LAST_NIGHT[0] not in pre_sess.summary and len(pre_sess.summary) == 116, len(pre_sess.summary))
ok("  and the boards change, because its lines are gone", pre != RECORDS)
post, post_sess, _ = run(events=edited(LAST_NIGHT[0], to_postponed))
ok("a postponed shell is not read either", LAST_NIGHT[0] not in post_sess.summary
   and len(post_sess.summary) == 116 and post == pre, len(post_sess.summary))

# --- no look-ahead, and where the window ends ---------------------------------
early, early_sess, early_log = run(today="2026-11-05")
ok("run for 11-05: nothing after 11-05 is requested or dated",
   early and all(s["date"] <= "2026-11-05" for r in early for s in r["series"])
   and len(early_sess.summary) == sum(1 for e in EVENTS if nba.slate_date(e) <= "2026-11-05"),
   len(early_sess.summary))
ok("  and its window ends on 11-05, a day the league played", "..2026-11-05," in early_log, early_log)
gap, gap_sess, gap_log = run(today="2026-11-20")
ok("ten days after the last game the boards are the same boards, not empty",
   gap == RECORDS and len(gap_sess.summary) == 117)
ok("  and the log says how old they are", "the league last played 10 days ago" in gap_log, gap_log)
none, none_sess, none_log = run(today="2026-10-25")
ok("before the first game there are no boards and no box-score requests",
   none == [] and none_sess.summary == [] and "no boards" in none_log, none_log)

# --- failure is loud ---------------------------------------------------------
ok("one failed box score raises: all of NBA's rows or none (invariant 5)",
   isinstance(raises(nba.fetch, CONFIG, today="2026-11-11",
                     session=_Session(fail=LAST_NIGHT[0])), RuntimeError))
broken = copy.deepcopy(SUMMARIES)
target = broken[LAST_NIGHT[0]]["boxscore"]["players"][0]["statistics"][0]
star = max((a for a in target["athletes"] if a["stats"]), key=lambda a: int(a["stats"][1]))
star["stats"][target["keys"].index("points")] = "--"
exc = raises(nba.fetch, CONFIG, today="2026-11-11", session=_Session(summaries=broken))
ok("a stat the feed left blank raises, naming the player and the field",
   isinstance(exc, ValueError) and star["athlete"]["displayName"] in str(exc)
   and "points" in str(exc), exc)
typo = copy.deepcopy(CONFIG)
typo["nba"]["stat_categories"][0]["mode"] = "rolling_summ"
ok("an unknown mode raises on a game day", isinstance(raises(nba.fetch, typo, today="2026-11-11",
                                                             session=_Session()), ValueError))
ok("  and on a day with nothing to read", isinstance(raises(nba.fetch, typo, today="2026-10-25",
                                                           session=_Session()), ValueError))

# --- registration, and the payload the site reads ----------------------------
ok("NBA's boards are registered: the fetcher, the gate, NFL still first",
   generate_stats.SPORT_FETCHERS.get("nba", {}).get("fetch") is nba.fetch
   and CONFIG["active_sports"][0] == "nfl" and "nba" in CONFIG["active_sports"],
   CONFIG["active_sports"])
cfg_keys = [c["key"] for c in NBA_CFG["stat_categories"]]
ok("  config, APPROVED_CATEGORIES and CATEGORY_META name the same seven rate boards",
   sorted(cfg_keys) == sorted(generate_stats.APPROVED_CATEGORIES["nba"]) and len(cfg_keys) == 7
   and all(generate_stats.CATEGORY_META[k]["kind"] == "rate" for k in cfg_keys), cfg_keys)
other = [k for s, ks in generate_stats.APPROVED_CATEGORIES.items() if s != "nba" for k in ks]
ok("  no NBA key collides with another sport's (CATEGORY_META is one namespace)",
   not set(cfg_keys) & set(other))
ok("  config's URLs and window are the module's",
   NBA_CFG["summary_url"] == nba.SUMMARY_URL and NBA_CFG["scoreboard_url"] == nba.SCOREBOARD_URL
   and NBA_CFG["window_games"] == nba.LEADERBOARD_WINDOW_GAMES)

generate_stats.index_category_labels(CONFIG)
ranked = generate_stats.rank_records(
    normalizer.normalize("nba", "NBA Regular Season", copy.deepcopy(RECORDS)), CONFIG.get("top_n", 10))
data = generate_stats.build_data(ranked, datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))
cats = (data["sports"].get("nba") or {}).get("categories") or []
ok("the payload carries seven boards in the configured order, ten deep",
   [c["key"] for c in cats] == generate_stats.APPROVED_CATEGORIES["nba"]
   and all(len(c["players"]) == 10 for c in cats), [(c["key"], len(c["players"])) for c in cats])
ok("  each says its real depth and its unit",
   all("Last 5 G" in c["sub"] and "{n}" not in c["sub"] and c["unit"].endswith("/G")
       and c["kind"] == "rate" for c in cats), [(c["sub"], c["unit"]) for c in cats])
ok("  every row has its club's abbreviation and colour",
   all(p["team_abbr"] == p["team"] and p["team_color"] for c in cats for p in c["players"]))
top = cats[0]["players"][0] if cats else {}
ok("  points leads with Doncic at 42.4",
   (top.get("entity"), top.get("value"), top.get("rank"), top.get("window")) ==
   ("Luka Doncic", 42.4, 1, 5), top)
ok("  the minutes context reaches the page: usage on the row, minutes on each game",
   top.get("usage") == {"minutes": 36.6, "per36": 41.7, "prev_minutes": 37.0, "prev_games": 2}
   and [g.get("min") for g in top.get("series") or []] == [int(l[3]["minutes"]) for l in raw_luka],
   top.get("usage"))
ok("the label for the sport is NBA", (data["sports"].get("nba") or {}).get("label") == "NBA")

print("nba leaderboards: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nba leaderboards: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
