"""Regression tests for backtest_season.py's exit code.

Run: python3 -m tools.verify.test_backtest_season   (from the repo root)

WHY THIS IS PINNED. From 92d717e (2026-08-27) until 2026-09-24 every date of
the MLB season backtest raised TypeError, its per-date `except Exception`
logged and skipped it, and the script exited 0: "0 dates graded, 7 skipped".
It was found only because a migration tried to use it as a before/after check
and would otherwise have compared nothing with nothing. A run that skipped
every date has measured nothing, and must say so with its exit code.

Pinned, all through the real main() with only backtest_date stubbed (so no
network, no config change, output to a temp file):

  * every date skipped -> exit 1, whatever the exception type;
  * every date an off day ("no_games") -> exit 0. An offseason range is a
    correct empty answer, not a failure, and must not trip the same alarm;
  * some dates skipped, some graded -> exit 0, with a WARNING line naming
    how many dates the record does not cover;
  * nothing skipped -> exit 0 and no warning.

Sabotage-checked when written: removing the all-skipped branch fails exactly
the four all-skipped assertions (exit code and message, for both exception
types; 4 of 10); counting off days as skips fails exactly the two off-day
assertions (2 of 10).
"""

import contextlib
import io
import os
import sys
import tempfile

import requests

import backtest_season

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append("%s  (%s)" % (name, detail) if detail else name)


def _run(behaviour):
    """main() over a fixed 4-date range, with backtest_date replaced by
    `behaviour(date)`. Returns (exit code, captured stdout)."""
    real = backtest_season.backtest_date
    backtest_season.backtest_date = (
        lambda date, config, session, base_url, cache, min_score: behaviour(date, cache))
    out = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            code = backtest_season.main(["--start", "2026-08-01", "--end", "2026-08-04",
                                         "--out", os.path.join(tmp, "bt.jsonl")])
    finally:
        backtest_season.backtest_date = real
    return code, out.getvalue()


def _raise(exc):
    def fn(date, cache):
        raise exc
    return fn


def test_every_date_skipped_fails():
    # The real 2026-08-27 failure was a TypeError; a network error is the other
    # path main() catches separately. Both must fail the run.
    for label, exc in (("TypeError", TypeError("'NoneType' object is not subscriptable")),
                       ("RequestException", requests.RequestException("read timed out"))):
        code, out = _run(_raise(exc))
        check("all 4 dates skipped by %s -> exit 1" % label, code == 1, "exit %r" % code)
        check("all-skipped run explains itself (%s)" % label,
              "graded nothing" in out and "all 4 date(s)" in out, out[-300:])


def test_an_offseason_range_is_not_a_failure():
    code, out = _run(lambda date, cache: ([], cache, "no_games"))
    check("all 4 dates off days -> exit 0", code == 0, "exit %r" % code)
    check("off days print no skip warning", "WARNING" not in out and "graded nothing" not in out)


def test_a_partial_skip_warns_but_passes():
    def some(date, cache):
        if date == "2026-08-02":
            raise TypeError("boom")
        return [], cache, "no_picks"
    code, out = _run(some)
    check("1 of 4 dates skipped -> exit 0", code == 0, "exit %r" % code)
    check("partial skip names how many dates are missing",
          "WARNING -- 1 of 4 date(s) skipped" in out, out[-300:])


def test_a_clean_run_is_quiet():
    code, out = _run(lambda date, cache: ([], cache, "no_picks"))
    check("no skips -> exit 0", code == 0, "exit %r" % code)
    check("no skips -> no warning", "WARNING" not in out)


def main():
    for fn in (test_every_date_skipped_fails,
               test_an_offseason_range_is_not_a_failure,
               test_a_partial_skip_warns_but_passes,
               test_a_clean_run_is_quiet):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in sorted(set(failures)):
            print("  " + f)
        return 1
    print("backtest season: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
