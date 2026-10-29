"""Regression tests for standing a sport down (taking it out of
`active_game_sports`) without stranding its last pick or redding its grader.

Run: python3 -m tools.verify.test_stand_down   (from the repo root)

WHY THIS IS PINNED. MLB was stood down on 2026-10-25, in the middle of the
World Series. Until then signal_report.py read neither gate (0 occurrences of
either key), which was harmless while every graded sport was also active.
Out of the gate, generate_insights stops building, clearing or rolling MLB's
partition forward, so the partition freezes as the last pre-flip run left it
-- on the day of the flip that was a real, displayed pick (TB @ MIL, NRFI,
slate 2026-10-24). Two silent failures were waiting either side of that:

  * STRANDED: if the grader skipped a stood-down sport outright, that pick
    would never be settled, and nothing would say so.
  * FALSE GAP: if it graded a stood-down sport as before, the strict MLB step
    would fetch each remaining World Series slate, find games the frozen store
    does not cover, write a `no_store` row claiming a hole in the record and
    red the workflow daily -- then go quiet all winter and red again on
    Opening Day 2027.

So five behaviours are pinned, on a REAL store and a REAL final:

  1. A stood-down sport whose frozen store COVERS the date grades it exactly
     as an active sport would: the two picks, with the verdicts the live
     ledger independently recorded for them on 2026-09-24 (both MISS).
  2. The same date already graded is not graded again (the frozen store keeps
     covering it for every later cron entry that day), and makes no fetch.
  3. A store that covers some OTHER date exits clean, writes NO row and makes
     NO schedule fetch -- the stand-down's normal day.
  4. An EMPTY partition is the same.
  5. The contrast that makes 3 mean something: the identical inputs with the
     sport ACTIVE still record `no_store` and fail, unchanged.
Plus the gate's one definition: configured_game_sports' resolution order, and
that generate_insights._active_game_sports is built on it.

The fixture is REAL: the store entries are commit f3bf07f's mlb partition
(the last committed store covering 2026-09-23), trimmed to the fields grading
reads, in stand_down_fixture.json; the finals are the 2026-09-23 StatsAPI
games already captured in season_phase_fixture.json. Nothing hand-written.
The live data/ stores and ledger are never opened -- every grading run here
happens in a scratch directory with its own config, store and ledger.

Sabotage-checked when written (2026-10-25, PYTHONDONTWRITEBYTECODE=1 and
__pycache__ cleared, see CLAUDE.md), measured rather than predicted:
  * disabling the stand-down branch in signal_report.main fails 9 of 31:
    behaviour 3 and 4 entirely (exit 2, a `no_store` row, a fetch -- the
    World Series false gap, reproduced), behaviour 2's no-append and no-fetch
    (the frozen store let a second same-day run append both rows AGAIN: 4
    rows, so that risk is real, not hypothetical), and behaviour 1's message.
    Behaviour 1's verdicts and 5 still pass, as they must: that is the old
    behaviour for a covered date and for an active sport.
  * returning EXIT_OK at the top of the branch (skip a stood-down sport
    outright) fails 5 of 31: all four of behaviour 1 -- the stranded pick --
    plus behaviour 3's message, which that early return never prints.
  * resolving the gate with `or` instead of `is None` (an explicit empty list
    widens to everything) fails exactly the three kill-switch assertions,
    3 of 31.
Restored: 31 checks pass.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import signal_report as sr  # noqa: E402
import generate_insights as gi  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


STORE = json.load(open(os.path.join(HERE, "stand_down_fixture.json")))["mlb"]
_PHASE_FX = json.load(open(os.path.join(HERE, "season_phase_fixture.json")))["mlb"]
FINALS = {str(g["gamePk"]): g for g in _PHASE_FX["2026-09-23"]}
# A real slate the store does NOT hold (a 2025 wild-card date, gamePk 813071):
# "the league played and nothing we stored covers it" -- the World Series case.
OTHER_DATE = "2025-10-01"
OTHER_SLATE = {str(g["gamePk"]): g for g in _PHASE_FX[OTHER_DATE]}
# What the live ledger recorded for these two picks (run 2026-09-24T05:29:36Z),
# written down here rather than read from data/ -- see the module docstring.
RECORDED = {("824223", "moneyline", "DET"): "MISS", ("824785", "nrfi_yrfi", "YRFI"): "MISS"}

BASE_CONFIG = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))


def grade(date, store, active, ledger_rows=(), slate=None, argv_extra=()):
    """One default-shaped (recordable) MLB grading run in a scratch directory.
    `active` puts mlb in or out of active_game_sports; everything else is the
    real config. Returns (exit_code, stdout, ledger rows after, fetch calls)."""
    cfg = copy.deepcopy(BASE_CONFIG)
    others = [s for s in (cfg.get("active_game_sports") or []) if s != "mlb"]
    cfg["active_game_sports"] = (["mlb"] + others) if active else others
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "data"))
    yaml.safe_dump(cfg, open(os.path.join(d, "config.yaml"), "w"))
    json.dump({"mlb": store}, open(os.path.join(d, "data", "insights.games.json"), "w"))
    with open(os.path.join(d, "data", "signal_report_history.jsonl"), "w") as f:
        for r in ledger_rows:
            f.write(json.dumps(r) + "\n")
    calls = []

    def fake_fetch(session, config, when):
        calls.append(when)
        return dict(FINALS if slate is None else slate)

    adapter = sr.SPORT_ADAPTERS["mlb"]
    real = adapter["fetch_slate"]
    adapter["fetch_slate"] = fake_fetch
    cwd, buf = os.getcwd(), io.StringIO()
    os.chdir(d)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = sr.main(["--date", date] + list(argv_extra))
    except SystemExit as e:
        code = e.code
    finally:
        os.chdir(cwd)
        adapter["fetch_slate"] = real
    after = [json.loads(l) for l in open(os.path.join(d, "data", "signal_report_history.jsonl"))
             if l.strip()]
    return code, buf.getvalue(), after, calls


def _picks(rows):
    return {(str(r.get("gamePk")), r.get("bet_type"), r.get("side")): r.get("verdict")
            for r in rows if sr._has_pick(r)}


# ------------------------------------------------- 1. the stranded-pick path
code, out, rows, calls = grade("2026-09-23", STORE, active=False)
ok("stood down, store covers the date: exits clean", code == sr.EXIT_OK, (code, out[-300:]))
ok("  and grades both stored picks", len(_picks(rows)) == 2, rows)
ok("  with the verdicts the live ledger recorded (both MISS)", _picks(rows) == RECORDED, _picks(rows))
ok("  and says why a stood-down sport is grading", "grading what it shipped" in out, out[:300])

# Exactly what an ACTIVE sport writes for the same inputs -- not a lookalike.
acode, aout, arows, _ = grade("2026-09-23", STORE, active=True)
strip = lambda rs: [{k: v for k, v in r.items() if k != "run_id"} for r in rs]  # noqa: E731
ok("  rows identical to an active sport's on the same store (run_id aside)",
   acode == sr.EXIT_OK and strip(rows) == strip(arows), (acode, len(rows), len(arows)))

# ------------------------------------------ 2. the same date, already graded
code2, out2, rows2, calls2 = grade("2026-09-23", STORE, active=False, ledger_rows=rows)
ok("already graded, store still covers it: exits clean", code2 == sr.EXIT_OK, (code2, out2[-200:]))
ok("  appends nothing (no second run of the same rows)", len(rows2) == len(rows), len(rows2))
ok("  and makes no schedule fetch", calls2 == [], calls2)

# --------------------------------- 3. the stand-down's normal day: other date
code3, out3, rows3, calls3 = grade(OTHER_DATE, STORE, active=False, slate=OTHER_SLATE)
ok("stood down, store covers another date: exits clean", code3 == sr.EXIT_OK, (code3, out3[-200:]))
ok("  writes NO row (not no_store: absence by design is not a gap)", rows3 == [], rows3)
ok("  makes NO schedule fetch (no StatsAPI dependency all winter)", calls3 == [], calls3)
ok("  and names the frozen store's date", "2026-09-23" in out3, out3[:200])

# ------------------------------------------------ 4. an empty partition
code4, out4, rows4, calls4 = grade(OTHER_DATE, {}, active=False, slate=OTHER_SLATE)
ok("stood down, EMPTY partition: exits clean", code4 == sr.EXIT_OK, (code4, out4[-200:]))
ok("  writes no row", rows4 == [], rows4)
ok("  and makes no schedule fetch", calls4 == [], calls4)

# -------------------------------- 5. the contrast: same inputs, sport ACTIVE
code5, out5, rows5, calls5 = grade(OTHER_DATE, STORE, active=True, slate=OTHER_SLATE)
ok("ACTIVE, store covers another date, games played: still FAILS", code5 != sr.EXIT_OK, code5)
ok("  and still records the gap as no_store",
   [r.get("status") for r in rows5] == [sr.STATUS_NO_STORE], rows5)
ok("  having fetched the schedule to find out", calls5 == [OTHER_DATE], calls5)
code6, _, rows6, calls6 = grade(OTHER_DATE, {}, active=True, slate=OTHER_SLATE)
ok("ACTIVE, empty partition, games played: still records no_store",
   code6 != sr.EXIT_OK and [r.get("status") for r in rows6] == [sr.STATUS_NO_STORE],
   (code6, rows6))

# A read-only look is not a recording run: it gets the old answer, not the
# stand-down's silence.
code7, out7, rows7, calls7 = grade(OTHER_DATE, STORE, active=False, slate=OTHER_SLATE,
                                   argv_extra=["--no-record"])
ok("--no-record on a stood-down sport still reports the mismatch", code7 != sr.EXIT_OK, code7)
ok("  and writes nothing", rows7 == [], rows7)

# ------------------------------------------------ the gate's one definition
cgs, act = sr.configured_game_sports, sr.is_game_sport_active
ok("configured_game_sports reads active_game_sports when present",
   cgs({"active_game_sports": ["epl"], "active_sports": ["mlb"]}) == ["epl"])
ok("  an explicit EMPTY list is the kill switch, not a fallback",
   cgs({"active_game_sports": [], "active_sports": ["mlb"]}) == [])
ok("  ...so nothing is active under it", not act({"active_game_sports": [], "active_sports": ["mlb"]}, "mlb"))
ok("  falls back to active_sports when the key is absent", cgs({"active_sports": ["mlb"]}) == ["mlb"])
ok("  None when neither names a sport (every builder is meant)", cgs({}) is None and act({}, "mlb"))
ok("  an empty active_sports keeps its old widening meaning",
   cgs({"active_sports": []}) is None)
ok("generate_insights._active_game_sports is built on it",
   gi._active_game_sports({"active_game_sports": ["epl", "nope"]}) == ["epl"]
   and gi._active_game_sports({}) == list(gi.GAME_BUILDERS)
   and gi._active_game_sports({"active_game_sports": []}) == [])

# ------------------------------------------------ the production decision
ok("production config: mlb is stood down from the picks gate",
   not act(BASE_CONFIG, "mlb") and "mlb" not in gi._active_game_sports(BASE_CONFIG))
ok("  and from the leaderboards gate (inactive means nothing at all)",
   "mlb" not in (BASE_CONFIG.get("active_sports") or []))
ok("  while its config block, and so its grading adapter, stays",
   "mlb" in BASE_CONFIG and "mlb" in sr.SPORT_ADAPTERS)


print("stand down: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "stand down: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
