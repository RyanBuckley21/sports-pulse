"""Regression tests for the NBA price capture (nba_price_capture.py).

Run: python3 -m tools.verify.test_nba_price_capture   (from the repo root)

WHY THIS IS PINNED. NBA prices exist only before tip and are gone for good
afterwards (ESPN strips the odds block at completion), so every mistake here
is unrecoverable AND silent -- the workflow stays green while the file fills
with the wrong thing, or with nothing:

  * WHAT IS KEPT. A row needs status "pre" and an odds block, and nothing
    else. The counted-game rule must NOT drop rows (playoff and play-in types
    were never captured, so a filter would discard exactly those), and a game
    that has started must never be stored as a pre-game price.
  * CHANGE-ONLY ROWS, PLUS A RUN ROW. An unchanged quote writes no price row,
    a moved one writes exactly one, and every run that saw a price says which
    ids it saw -- that is what tells "line did not move" from "scheduler
    dropped the runs".
  * APPEND-ONLY. Existing bytes are never rewritten, a cut-off last line
    cannot fuse with a new row, and an unparseable line neither stops the
    capture nor gets "repaired".
  * FAILURE WRITES NOTHING WRONG. Every request failing exits 1 with the file
    untouched; some failing still captures the rest and names what failed. An
    idle run (nothing priced) leaves the file byte-identical.
  * CAPTURE ONLY. NBA stays out of both gates, and the module imports no
    builder, grader or store.

Fixture: tools/verify/nba_games_fixture.json, REAL and unedited (13 events
captured 2026-10-30/31; its _provenance labels name the three pre-game events
that carried DraftKings prices, and this suite reads the expected set FROM
those labels). Four edges were never observed, so each is built by editing
exactly ONE field of a real event, documented at its assertion: a moved line
(one close price), a started game still carrying odds (status state), a
moneyline reading OFF (both close cells -- one edit, the same field on each
side), and a priced event of an unknown competition type (type id). No
network; every write goes to a temp directory, never to data/.

Sabotage-checked when written (2026-11-10, PYTHONDONTWRITEBYTECODE=1, source
byte-identical afterwards), failures measured, of 58:
  S1  pre-game guard dropped                       2 fail
  S2  counted-game rule used as a filter           2 fail  (unknown type lost)
  S3  OFF blocks skipped                           1 fail
  S4  every observation written                    8 fail
  S5  change judged against the FIRST row          3 fail  (a line moving back)
  S6  no run row                                  10 fail
  S7  run row written when nothing priced          3 fail  (idle runs commit)
  S8  file rewritten instead of appended           6 fail
  S9  torn-line newline guard dropped              2 fail
  S10 an unparseable line is fatal                 4 fail
  S11 total failure exits 0                        1 fail
  S12 one failed day aborts the run                3 fail
  S13 days taken from the UTC date                 1 fail
  S14 yesterday not requested                      4 fail
  S15 cross-day duplicate ids kept                 1 fail
  S16 --dry-run writes                             1 fail
  S17 open prices dropped from the row             1 fail
S6 and S10 first CRASHED the suite (KeyError; JSONDecodeError) instead of
reporting failures; the run-row and reader lookups were made tolerant so a
broken rule is named, not just a traceback.
"""

import contextlib
import copy
import datetime
import io
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import nba_price_capture as cap  # noqa: E402
import yaml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


_FX = json.load(open(os.path.join(HERE, "nba_games_fixture.json")))
CASES = _FX["_provenance"]["cases"]
EVENTS = {k: v for k, v in _FX.items() if not k.startswith("_")}
ORDERED = [EVENTS[k] for k in sorted(EVENTS)]

# The capture moment: 2026-10-31 14:00 UTC, i.e. 10:00 AM Eastern on the day
# the fixture's three priced games tip (19:00Z, 22:00Z, 23:00Z).
NOW = datetime.datetime(2026, 10, 31, 14, 0, tzinfo=datetime.timezone.utc)


def quiet_run(*args, **kwargs):
    """cap.run with its progress lines swallowed, so the suite prints one line."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cap.run(*args, **kwargs)

LATER = NOW + datetime.timedelta(hours=3)


def last_run(rows):
    """The last run row, or {} -- so a capture that writes none is reported as
    failed checks rather than a KeyError (S6 first crashed the suite)."""
    found = [r for r in rows if r.get("type") == "run"]
    return found[-1] if found else {}


def safe_read(path):
    """cap.read_rows, or (None, the error) if it raises -- so a reader that
    dies on a bad line is a named failure, not a traceback (S10)."""
    try:
        return cap.read_rows(path)
    except Exception as exc:  # noqa: BLE001
        return None, repr(exc)


def price_rows(rows):
    return [r for r in rows if r.get("type") == "price"]


def run_rows(rows):
    return [r for r in rows if r.get("type") == "run"]


class _Resp:
    def __init__(self, events):
        self._events = events

    def raise_for_status(self):
        pass

    def json(self):
        return {"events": self._events}


class _Session:
    """Answers each `dates=` key from a table; a key mapped to an Exception
    raises it, exactly where a real outage would."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}, url=url))
        answer = self.table.get(params["dates"], [])
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)


# ------------------------------------------------------- 1. what is observed
# Expected set from the CAPTURE'S OWN LABELS, not from the code under test.
want = {k for k, label in CASES.items() if "pre-game -- DK open+close" in label}
ok("the fixture labels three priced pre-game events", len(want) == 3, want)
obs = cap.observe(ORDERED, NOW)
got = {r["event_id"] for r in obs}
ok("observe() keeps exactly the priced pre-game events", got == want, (got, want))
ok("  a completed game (odds block gone) yields no row",
   not got & {"401909881", "401909882"})
ok("  a pre-game event with no odds yet yields no row", "401909907" not in got)

by_id = {r["event_id"]: r for r in obs}
utah = by_id.get("401909896", {})
ok("POR @ UTAH: both sides' moneylines, as integers",
   (utah.get("home"), utah.get("home_ml"), utah.get("away"), utah.get("away_ml"))
   == ("UTAH", 185, "POR", -225), utah)
ok("  provider, total and spread are carried",
   (utah.get("provider"), utah.get("total"), utah.get("spread")) == ("DraftKings", 232.5, 6.5), utah)
ok("  slate date is the Eastern date (19:00Z on 10-31 is 3 PM ET, 10-31)",
   utah.get("slate_date") == "2026-10-31" and utah.get("start") == "2026-10-31T19:00Z", utah)
ok("  captured_at is the run's UTC stamp", utah.get("captured_at") == "2026-10-31T14:00:00Z", utah)
ok("  raw classification is stored beside the price",
   (utah.get("season_type"), utah.get("comp_type_id"), utah.get("counted")) == (2, "1", True), utah)
no = by_id.get("401909898", {})
ok("ATL @ NO: open and close are different real numbers, both kept",
   (no.get("home_ml_open"), no.get("home_ml"), no.get("away_ml_open"), no.get("away_ml"))
   == (130, 142, -155, -170), no)
ok("every row is typed and versioned",
   all(r["type"] == "price" and r["schema_version"] == cap.SCHEMA_VERSION for r in obs))

# EDGE, one field edited: a STARTED game that still carries an odds block.
# Never observed (ESPN drops NBA odds; the fixture's finished games have none),
# so 401909896's status.type.state is set to "in" and nothing else is touched.
started = copy.deepcopy(EVENTS["401909896"])
started["competitions"][0]["status"]["type"]["state"] = "in"
ok("a started game is never stored as a pre-game price (state 'in', odds present)",
   cap.observe([started], NOW) == [])
finished = copy.deepcopy(EVENTS["401909896"])
finished["competitions"][0]["status"]["type"]["state"] = "post"
ok("  nor a finished one (state 'post', odds present)", cap.observe([finished], NOW) == [])

# EDGE, one field edited: a priced pre-game event of a competition type the
# counted-game rule does not know (playoff/play-in ids were never captured).
# 401909897's type id is set to "99". It must be KEPT, with counted False.
unknown = copy.deepcopy(EVENTS["401909897"])
unknown["competitions"][0]["type"]["id"] = "99"
u = cap.observe([unknown], NOW)
ok("a priced game of an unknown competition type is KEPT, not filtered",
   len(u) == 1 and u[0]["comp_type_id"] == "99", u)
ok("  and is marked counted False, so a reader can apply the rule later",
   bool(u) and u[0]["counted"] is False, u)

# EDGE, one field edited (the same cell on each side): a moneyline reading OFF.
off = copy.deepcopy(EVENTS["401909897"])
for side in ("home", "away"):
    off["competitions"][0]["odds"][0]["moneyline"][side]["close"]["odds"] = "OFF"
o = cap.observe([off], NOW)
ok("an OFF moneyline is stored as a refusal to quote, not skipped",
   len(o) == 1 and o[0]["moneyline_off"] is True
   and o[0]["home_ml"] is None and o[0]["away_ml"] is None, o)

# ------------------------------------------- 2. change-only rows + a run row
DAYS = ["2026-10-30", "2026-10-31", "2026-11-01"]
first = cap.plan(obs, [], NOW, DAYS, [], len(ORDERED))
ok("first sighting: one price row per priced event", len(price_rows(first)) == 3, first)
ok("  and exactly one run row, written last",
   len(run_rows(first)) == 1 and first[-1]["type"] == "run", first)
run1 = last_run(first)
ok("  the run row lists every id seen priced", set(run1.get("priced_ids") or []) == want, run1)
ok("  the run row counts events seen, new rows, and the days asked",
   (run1.get("events"), run1.get("new_price_rows"), run1.get("days"), run1.get("failed_days"))
   == (13, 3, DAYS, []), run1)

again = cap.plan(cap.observe(ORDERED, LATER), first, LATER, DAYS, [], len(ORDERED))
ok("an unchanged quote writes NO new price row", price_rows(again) == [], again)
ok("  but the run row still says the three were seen, at the later time",
   len(run_rows(again)) == 1 and set(last_run(again).get("priced_ids") or []) == want
   and last_run(again).get("captured_at") == "2026-10-31T17:00:00Z"
   and last_run(again).get("new_price_rows") == 0,
   again)

# EDGE, one field edited: a line that MOVED between two captures. The fixture
# is one moment in time, so PHI @ CHA's away close is moved -120 -> -135.
moved_events = copy.deepcopy(ORDERED)
for e in moved_events:
    if e["id"] == "401909897":
        e["competitions"][0]["odds"][0]["moneyline"]["away"]["close"]["odds"] = "-135"
moved = cap.plan(cap.observe(moved_events, LATER), first, LATER, DAYS, [], len(ORDERED))
ok("a moved line writes exactly ONE new price row, for that game",
   [r["event_id"] for r in price_rows(moved)] == ["401909897"], moved)
ok("  carrying the new number and the later stamp",
   bool(price_rows(moved)) and price_rows(moved)[0]["away_ml"] == -135
   and price_rows(moved)[0]["captured_at"] == "2026-10-31T17:00:00Z", moved)
history = first + moved
ok("  the first row for the game is still its first capture (-120)",
   next(r for r in price_rows(history) if r["event_id"] == "401909897")["away_ml"] == -120)
ok("  and latest_prices() reads the close as the last row (-135)",
   cap.latest_prices(history)["401909897"]["away_ml"] == -135)
back = cap.plan(cap.observe(ORDERED, LATER), history, LATER, DAYS, [], len(ORDERED))
ok("a line moving BACK is a change against the latest row, not the first",
   [r["event_id"] for r in price_rows(back)] == ["401909897"], back)

ok("nothing priced: the plan is empty (no run row, no commit)",
   cap.plan([], first, NOW, DAYS, [], 13) == [])
dup = cap.plan(obs + [copy.deepcopy(obs[0])], [], NOW, DAYS, [], 14)
ok("one id listed under two days in a run is written once", len(price_rows(dup)) == 3, dup)

# ------------------------------------------------------------ 3. append-only
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "sub", "nba_prices.jsonl")
    cap.append_rows(path, first)
    one = open(path, "rb").read()
    ok("append creates the file (and its directory) with one JSON object per line",
       len(one.splitlines()) == 4 and all(json.loads(l) for l in one.splitlines()), one[:80])
    cap.append_rows(path, moved)
    two = open(path, "rb").read()
    ok("a second append leaves every existing byte in place", two.startswith(one))
    ok("  and adds only the new rows", len(two.splitlines()) == 4 + len(moved))
    cap.append_rows(path, [])
    ok("appending nothing does not touch the file", open(path, "rb").read() == two)
    rows, bad = cap.read_rows(path)
    ok("read_rows returns what was written, in order", rows == first + moved and bad == [])

    # A cut-off earlier write: the file ends mid-line with no newline.
    torn = os.path.join(tmp, "torn.jsonl")
    with open(torn, "wb") as fh:
        fh.write(one + b'{"type": "price", "event_id": "40190')
    before = open(torn, "rb").read()
    cap.append_rows(torn, moved)
    after = open(torn, "rb").read()
    ok("a torn last line is left in place, byte for byte", after.startswith(before))
    rows, bad = safe_read(torn)
    ok("  the new rows do not fuse onto it: every appended row parses",
       rows is not None
       and [r for r in rows if r.get("captured_at") == "2026-10-31T17:00:00Z"] == moved, bad)
    ok("  and the torn line is reported, not fatal and not dropped", bad == [5], bad)
    ok("  change detection still works across the bad line",
       rows is not None
       and price_rows(cap.plan(cap.observe(moved_events, LATER), rows, LATER, DAYS, [], 13)) == [])
    try:
        torn_code = quiet_run(_Session({"20261031": moved_events}), path=torn, now=LATER)
    except Exception as exc:  # noqa: BLE001
        torn_code = repr(exc)
    ok("  and a whole run over the torn file still exits 0 (capture never stops on it)",
       torn_code == 0, torn_code)
    ok("a missing file reads as empty", cap.read_rows(os.path.join(tmp, "absent.jsonl")) == ([], []))


# ------------------------------------------------ 4. the run: fetch and fail
ok("the days asked are yesterday, today and tomorrow, EASTERN",
   cap.slate_days(NOW) == DAYS, cap.slate_days(NOW))
# 02:30 UTC on Nov 1 is still 10:30 PM Eastern on Oct 31 -- the hour most games
# are tipping. A UTC date here would ask for Oct 31..Nov 2 and file tonight's
# late games under "yesterday" a day early.
late = datetime.datetime(2026, 11, 1, 2, 30, tzinfo=datetime.timezone.utc)
ok("  at 02:30 UTC the Eastern day has not turned over", cap.slate_days(late) == DAYS,
   cap.slate_days(late))

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "nba_prices.jsonl")
    # The same events listed under two days, as a UTC-vs-Eastern boundary would.
    sess = _Session({"20261031": ORDERED, "20261101": ORDERED})
    code = quiet_run(sess, path=path, now=NOW)
    rows, _ = cap.read_rows(path)
    ok("run(): exit 0, three price rows and one run row on file",
       code == 0 and len(price_rows(rows)) == 3 and len(run_rows(rows)) == 1, (code, rows))
    ok("  one single-day request per day, never a range",
       [c["dates"] for c in sess.calls] == ["20261030", "20261031", "20261101"], sess.calls)
    ok("  every request carries limit=500 (invariant 11)",
       all(c.get("limit") == 500 for c in sess.calls), sess.calls)
    ok("  an event listed under two days is counted once",
       last_run(rows).get("events") == 13, run_rows(rows))

    snapshot = open(path, "rb").read()
    code = quiet_run(_Session({k: OSError("down") for k in ("20261030", "20261031", "20261101")}),
                   path=path, now=LATER)
    ok("every request failing: exit 1", code == 1, code)
    ok("  and the file is byte-identical", open(path, "rb").read() == snapshot)

    code = quiet_run(_Session({"20261030": OSError("down"), "20261031": moved_events}),
                   path=path, now=LATER)
    rows, _ = cap.read_rows(path)
    ok("one day failing: exit 0 and the other days are still captured",
       code == 0 and bool(price_rows(rows)) and price_rows(rows)[-1]["away_ml"] == -135,
       (code, rows[-2:]))
    ok("  the run row names the day that failed",
       last_run(rows).get("failed_days") == ["2026-10-30"], last_run(rows))

    snapshot = open(path, "rb").read()
    idle = [EVENTS["401909881"], EVENTS["401909907"]]      # finished; unpriced
    code = quiet_run(_Session({"20261031": idle}), path=path, now=LATER)
    ok("an idle run (nothing priced): exit 0, file byte-identical",
       code == 0 and open(path, "rb").read() == snapshot)

    snapshot = open(path, "rb").read()
    code = quiet_run(_Session({"20261031": ORDERED}), path=path, now=LATER, dry_run=True)
    ok("--dry-run writes nothing", code == 0 and open(path, "rb").read() == snapshot)

    full = _Session({"20261031": [EVENTS["401909896"]] * 500})
    code = quiet_run(full, path=os.path.join(tmp, "full.jsonl"), now=NOW)
    ok("a full page (500 events) is refused as possibly truncated, not used",
       code == 0 and not os.path.exists(os.path.join(tmp, "full.jsonl")), code)

# ------------------------------------------------------------ 5. capture only
with open(os.path.join(_ROOT, "config.yaml")) as fh:
    CONFIG = yaml.safe_load(fh)
# When this suite was written NBA was in neither gate. Its picks and then its
# leaderboards went live on 2026-11-11; what must stay true for the capture is
# that it is independent of both -- it reads no gate at all. (This used to
# assert NBA was absent from active_sports. That was a fact about the day, not
# about the capture, and stopped being true when the boards shipped.)
ok("the capture reads no gate: it would run the same with NBA switched off",
   "active_game_sports" not in open(os.path.join(_ROOT, "nba_price_capture.py")).read()
   .split('"""', 2)[2] and "active_sports" not in open(os.path.join(_ROOT, "nba_price_capture.py")).read()
   .split('"""', 2)[2])
SOURCE = open(os.path.join(_ROOT, "nba_price_capture.py")).read()
_imports = [l.strip() for l in SOURCE.splitlines() if l.startswith(("import ", "from "))]
ok("the module imports no builder, grader or store writer",
   not any(name in line for line in _imports
           for name in ("generate_insights", "generate_stats", "signal_report",
                        "nba_grading", "signal_core", "bet_board")), _imports)
ok("its only write target is its own file", cap.PRICES_PATH == "data/nba_prices.jsonl")
WORKFLOW = open(os.path.join(_ROOT, ".github", "workflows", "capture-nba-prices.yml")).read()
_adds = [l.strip() for l in WORKFLOW.splitlines() if l.strip().startswith("git add")]
ok("the workflow stages that one file and nothing else",
   _adds == ["git add data/nba_prices.jsonl"], _adds)
ok("the workflow runs the capture module", "python3 nba_price_capture.py" in WORKFLOW)

print("nba price capture: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nba price capture: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
