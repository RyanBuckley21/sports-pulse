"""The slate-date boundary, and the two failures that came from getting it wrong.

WHAT HAPPENED, because these tests only make sense against it. generate_insights
stamped the games store with `generated_at.date()` -- a UTC date -- while
signal_report graded `yesterday` in US/Eastern. Those agree for any run between
about 04:00 and 23:59 UTC, which the workflow's 13:40/15:40 crons comfortably
were. Then GitHub began firing them nine to eleven hours late:

  run 62  2026-08-27 23:02Z  = 19:02 ET on the 27th  -> yesterday = 08-26  graded
  run 63  2026-08-28 00:34Z  = 20:34 ET on the 27th  -> yesterday = 08-26  again
  run 64  2026-08-28 23:11Z  = 19:11 ET on the 28th  -> yesterday = 08-27  GONE

Both runs of that cycle asked about the same day, so nothing ever asked about
2026-08-27 -- and run 63 rolled the store forward to a UTC date of 08-28,
taking the 27th's pre-game snapshot with it. Two days of MLB picks went
ungraded and were recorded as `no_store`.

Nothing threw. Every run exited the way it was designed to. That is why this is
measured here rather than reasoned about.

THREE GROUPS:

  slate_clock -- one definition of the boundary, pinned at the exact hours that
  broke. Eastern is not a preference: a 10pm ET game on the 28th is on the
  28th's slate and MLB's API returns it under date=2026-08-28, so a UTC date
  names the wrong slate for every late-evening run.

  slate_date -- the store now says which build it is from. `generated_at` could
  not answer that: it is carried forward per row, so on EPL's three-day and
  CFB's seven-day windows a fixture first seen on the 27th reports the 27th
  forever. Both readers (signal_report's diagnostic, generate_insights' overwrite
  guard) prefer the new field and fall back for old rows.

  empty slate -- a league that did not play is not a gap. That branch used to
  die and write a `no_store` row; on the real 08-26..28 window it wrote three
  for CFB and two for EPL on dates neither league had a fixture.

Offline: no network, no committed file is touched, and the one grading path
exercised uses a stub slate.
"""

import datetime
import io
import json
import os
import sys
import tempfile
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import slate_clock  # noqa: E402
import signal_report as sr  # noqa: E402
import generate_insights as gi  # noqa: E402

UTC = datetime.timezone.utc
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


# ------------------------------------------------------------- slate_clock
# The exact timestamps from the incident, plus the boundary either side.
CASES = [
    ("2026-08-27T23:02:51Z", datetime.datetime(2026, 8, 27, 23, 2, 51, tzinfo=UTC), "2026-08-27"),
    ("2026-08-28T00:34:53Z", datetime.datetime(2026, 8, 28, 0, 34, 53, tzinfo=UTC), "2026-08-27"),
    ("2026-08-28T23:11:24Z", datetime.datetime(2026, 8, 28, 23, 11, 24, tzinfo=UTC), "2026-08-28"),
    ("2026-08-29T00:04:38Z", datetime.datetime(2026, 8, 29, 0, 4, 38, tzinfo=UTC), "2026-08-28"),
    ("2026-08-29T03:59:00Z", datetime.datetime(2026, 8, 29, 3, 59, 0, tzinfo=UTC), "2026-08-28"),
    ("2026-08-29T04:00:00Z", datetime.datetime(2026, 8, 29, 4, 0, 0, tzinfo=UTC), "2026-08-29"),
    ("2026-08-29T13:40:00Z", datetime.datetime(2026, 8, 29, 13, 40, 0, tzinfo=UTC), "2026-08-29"),
]
for label, when, expect in CASES:
    got = slate_clock.eastern_date(when)
    ok("{} is slate {}".format(label, expect), got == expect, got)

# The whole bug in one assertion: the two runs that straddle UTC midnight are
# the SAME slate day, and .date() on the UTC stamp says otherwise.
a = datetime.datetime(2026, 8, 27, 23, 2, 51, tzinfo=UTC)
b = datetime.datetime(2026, 8, 28, 0, 34, 53, tzinfo=UTC)
ok("the two straddling runs share a slate date",
   slate_clock.eastern_date(a) == slate_clock.eastern_date(b))
ok("  ...which the UTC date does NOT (this was the bug)",
   a.date().isoformat() != b.date().isoformat())

try:
    slate_clock.eastern_date(datetime.datetime(2026, 8, 29, 0, 4))
    ok("a naive datetime raises", False, "it returned instead")
except ValueError:
    ok("a naive datetime raises", True)

ok("yesterday() is one day back from today()",
   (datetime.date.fromisoformat(slate_clock.eastern_date())
    - datetime.date.fromisoformat(slate_clock.yesterday())).days == 1)
ok("signal_report.yesterday_et delegates to it",
   sr.yesterday_et() == slate_clock.yesterday())

# generate_insights must use the same definition, not its own .date().
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "generate_insights.py")).read()
ok("generate_insights derives game_date from slate_clock",
   "slate_clock.eastern_date(generated_at)" in src)
ok("  and no longer from generated_at.date()",
   "generated_at.date().isoformat()" not in src)


# -------------------------------------------------------------- slate_date
GEN = "2026-08-27T23:03:13.516814+00:00"
new_row = {"slate_date": "2026-08-29", "generated_at": GEN}
old_row = {"generated_at": GEN}

ok("store_slate_dates prefers slate_date",
   sr.store_slate_dates({"1": new_row}) == ["2026-08-29"],
   sr.store_slate_dates({"1": new_row}))
ok("  and falls back to the generated_at day for pre-field rows",
   sr.store_slate_dates({"1": old_row}) == ["2026-08-27"],
   sr.store_slate_dates({"1": old_row}))
ok("  a mixed store reports both, sorted",
   sr.store_slate_dates({"1": new_row, "2": old_row}) == ["2026-08-27", "2026-08-29"])
ok("  a row with neither is dropped rather than reported as ''",
   sr.store_slate_dates({"1": {}}) == [])

ok("_store_covers_date prefers slate_date",
   gi._store_covers_date({"1": new_row}, "2026-08-29") is True)
ok("  ...and is NOT fooled by the carried-forward generated_at",
   gi._store_covers_date({"1": new_row}, "2026-08-27") is False)
ok("  falls back for pre-field rows",
   gi._store_covers_date({"1": old_row}, "2026-08-27") is True)
ok("  and still answers False for a date it does not hold",
   gi._store_covers_date({"1": new_row}, "2026-08-28") is False)

# The stamp itself: fresh every run, never carried, unlike generated_at beside it.
ent = {"away": {"abbr": "AWY"}, "home": {"abbr": "HME"}, "start": "7:05 PM ET",
       "venue": "V", "probables": None, "signals": [], "pulse": None,
       "betting_signals": {}, "standout": None, "status": "Preview"}
prior = {"1": {"generated_at": GEN, "slate_date": "2026-08-27",
               "story": "kept", "summary": None, "betting_note": None}}
out = gi._carry_forward_games_store({"1": ent}, prior, "2026-08-29T12:00:00+00:00", "2026-08-29")
ok("slate_date is stamped fresh on a carried-forward row",
   out["1"]["slate_date"] == "2026-08-29", out["1"]["slate_date"])
ok("  while generated_at IS carried (they answer different questions)",
   out["1"]["generated_at"] == GEN, out["1"]["generated_at"])


# ------------------------------------------------------------- empty slate
def run_grader(slate, argv, store):
    """main() with the slate fetch stubbed. Returns (exit_code, stdout)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"mlb": store}, f)
    real = sr.SPORT_ADAPTERS["mlb"]["fetch_slate"]
    sr.SPORT_ADAPTERS["mlb"]["fetch_slate"] = lambda session, config, date: slate
    # BOTH STREAMS: die() writes to stderr and the clean paths to stdout, and a
    # test that watched only one would call a fatal message "missing".
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = sr.main(argv + ["--store", path])
    except SystemExit as e:
        code = e.code
    finally:
        sr.SPORT_ADAPTERS["mlb"]["fetch_slate"] = real
        os.unlink(path)
    return code, buf.getvalue()

STORE = {"777": {"slate_date": "2026-08-29", "generated_at": GEN,
                 "away": {"abbr": "AWY"}, "home": {"abbr": "HME"},
                 "start": "7:05 PM ET", "status": "Preview",
                 "standout": {"bet_type": "moneyline", "side": "HME", "score": 80}}}

code, out = run_grader({}, ["--date", "2026-08-28", "--no-record"], STORE)
ok("an empty slate exits CLEAN, not fatal", code == sr.EXIT_OK, code)
ok("  and says the league did not play", "did not play" in out, out.strip()[:90])
ok("  without claiming a gap", "no_store" not in out and "no gamePk overlap" not in out,
   out.strip()[:90])

# The contrast that makes the above meaningful: a NON-empty slate with no
# overlap is still a real store/date mismatch and must still be fatal.
code, out = run_grader({"999": {}}, ["--date", "2026-08-28", "--no-record"], STORE)
ok("a non-empty slate with no overlap is STILL fatal", code != sr.EXIT_OK, code)
ok("  and names the mismatch", "no gamePk overlap" in out, out.strip()[-90:])
ok("  reporting the slate_date, not the generated_at day",
   "2026-08-29" in out and "covers 2026-08-27" not in out, out.strip()[-120:])


print("slate dates: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "slate dates: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
