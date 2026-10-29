"""Regression tests for the CFBD monthly budget (fetchers/cfb.py).

Run: python3 -m tools.verify.test_cfbd_budget   (from the repo root)

WHY THIS IS PINNED. The free CFBD tier is 1,000 calls a month and the only
guard was a per-RUN ceiling of 12, which allows 1,440 a month at four runs a
day. Nothing counted calls across runs, and no run logged them. September 2026
was reconstructed at about 86 calls by 09-25 (see CFBD_MONTHLY_BUDGET's
comment), comfortably inside the tier, but a cache that stops committing would
have drained the month with nothing saying so, and CFB form would have gone
stale for the rest of it -- the "lose a week" the owner asked to rule out.

What fails quietly, and is pinned here:
  * the count must SURVIVE every return path, including the early ones that
    replace the committed cfb cache (no schedule, no games in the window);
  * at the budget, the fetcher must make NO request -- not one more, and not
    an exception that takes the slate down;
  * a month with no stored count must start from the measured seed, not zero.

Since 2026-09-26 it also pins that a week is cached as soon as CFBD's data
covers every game in it, not only when cfbfastR's lagging `completed` flag
says so (that lag cost about 86 calls in September), and never with a game
missing. Removing the completeness rule fails 1 of 18; ignoring a missing
team's row fails 1.

Sabotage-checked when written (PYTHONDONTWRITEBYTECODE=1): capping at the
per-run ceiling only (ignoring the month) fails 4 of 15; dropping the count
from the no-schedule return fails 1; ignoring the seed fails 1; rebuilding the
cache from scratch in fetch_team_form_data fails 1.

NO NETWORK: a stub session stands in for requests, and CFB_ALLOW_CFBD and a
dummy CFBD_API_KEY are set only inside the test.
"""

import os
import sys

from fetchers import cfb

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + str(detail) + ")") if detail != "" else ""))


class _Session:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1

        class R:
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return []
        return R()


def _with_env(fn):
    saved = {k: os.environ.get(k) for k in ("CFB_ALLOW_CFBD", "CFBD_API_KEY")}
    os.environ["CFB_ALLOW_CFBD"] = "1"
    os.environ["CFBD_API_KEY"] = "test-key-not-real"
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_month_starts_from_the_measured_seed():
    check("September 2026 with no stored count starts at the measured 86",
          cfb.cfbd_month_used({}, "2026-09") == 86, cfb.cfbd_month_used({}, "2026-09"))
    check("a stored count wins over the seed",
          cfb.cfbd_month_used({"cfbd_usage": {"2026-09": 120}}, "2026-09") == 120)
    check("an unseeded month with no count starts at zero",
          cfb.cfbd_month_used({}, "2026-10") == 0)


def test_the_run_is_capped_by_what_the_month_has_left():
    cfb.reset_cfbd_budget()
    cfb.apply_monthly_budget({"cfbd_usage": {"2026-10": 0}}, "2026-10")
    check("a fresh month allows the full per-run ceiling",
          cfb._cfbd_calls["limit"] == cfb.MAX_CFBD_CALLS_PER_RUN, cfb._cfbd_calls["limit"])
    cfb.reset_cfbd_budget()
    cfb.apply_monthly_budget({"cfbd_usage": {"2026-10": cfb.CFBD_MONTHLY_BUDGET - 2}}, "2026-10")
    check("two calls short of the budget allows exactly two", cfb._cfbd_calls["limit"] == 2,
          cfb._cfbd_calls["limit"])

    def spend():
        s = _Session()
        got = [cfb._cfbd_get(s, "/ppa/games", {"year": 2026}) for _ in range(5)]
        return s, got
    s, got = _with_env(spend)
    check("at the budget, no further request goes out", s.calls == 2, s.calls)
    check("  and the refused calls return empty rather than raising", got[2:] == [[], [], []], got)
    check("  and the run is marked capped, so it says so once", cfb._cfbd_calls["capped"] is True)
    cfb.reset_cfbd_budget()
    cfb.apply_monthly_budget({"cfbd_usage": {"2026-10": cfb.CFBD_MONTHLY_BUDGET + 50}}, "2026-10")
    check("over the budget, the limit is zero, never negative", cfb._cfbd_calls["limit"] == 0)


def test_the_count_is_recorded_and_bounded():
    cfb.reset_cfbd_budget()
    cfb.apply_monthly_budget({"cfbd_usage": {"2026-09": 90}}, "2026-09")
    cfb._cfbd_calls["count"] = 3
    out = cfb.record_cfbd_usage({"cfbd_usage": {"2026-07": 5, "2026-08": 40, "2026-09": 90},
                                 "ppa": {"2026": {"1": {}}}}, "2026-09")
    check("this run's calls are added to the month", out["cfbd_usage"]["2026-09"] == 93, out["cfbd_usage"])
    check("only the current and previous month are kept",
          sorted(out["cfbd_usage"]) == ["2026-08", "2026-09"], out["cfbd_usage"])
    check("the week cache beside it is untouched", out.get("ppa") == {"2026": {"1": {}}})


def test_the_count_survives_the_early_returns():
    saved = cfb.get_schedule
    cfb.get_schedule = lambda session, season: []
    try:
        ents, cache, rows = cfb.build_game_entities({}, "2026-09-26", {"cfbd_usage": {"2026-09": 101}})
    finally:
        cfb.get_schedule = saved
    month = cfb._usage_month()
    check("no schedule: still no games", ents == {})
    check("  but the month's count is carried in the returned cache, not wiped",
          (cache.get("cfbd_usage") or {}).get(month) is not None, cache)


def test_the_week_cache_fetch_keeps_the_count():
    # The main return path: build_game_entities hands its cache through
    # fetch_team_form_data, which rebuilds the ppa/games_teams buckets. The
    # count must ride through that untouched. Every week requested is already
    # cached, so this makes no request at all.
    cache = {"cfbd_usage": {"2026-09": 91},
             "ppa": {"2026": {"1": {}}}, "games_teams": {"2026": {"1": {}}}}
    _, _, out = cfb.fetch_team_form_data(_Session(), 2026, [1], [], cache)
    check("the week-cache fetch carries the month's count through",
          out.get("cfbd_usage") == {"2026-09": 91}, out.get("cfbd_usage"))


class _CFBDStub:
    """Answers /ppa/games and /games/teams from the real cached 2026 rows in
    cfb_form_fixture.json, in CFBD's own row shapes, and counts the calls."""

    def __init__(self, fx, drop=None):
        self.fx, self.drop, self.calls = fx, drop, 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        fx, drop = self.fx, self.drop
        if url.endswith("/ppa/games"):
            body = [r for w in fx["ppa"] for r in cfb._ppa_rows_from_cache(fx["ppa"][w])
                    if not (drop and str(r["gameId"]) == drop[0] and r["team"] == drop[1])]
        else:
            body = cfb._stats_rows_from_cache(fx["games_teams"].get(str(params["week"]), {}))

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return body
        return R()


def test_a_week_is_cached_when_its_data_is_complete():
    # THE LAG: cfbfastR's `completed` flag runs days behind, so a week whose
    # CFBD data is already complete was re-fetched on every run until the flag
    # caught up (weeks 1-3 of 2026 each cached 5-6 days late). EDIT (one
    # field, in one row): one week-3 game's `completed` is set to FALSE --
    # exactly what the live flag read for days after the game -- so week 3 is
    # NOT final by the flag. Its start date is left real, and the staleness
    # fallback is 14 days, so the flag rule alone cannot cache it.
    import json as _json
    fx = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfb_form_fixture.json")))
    sched = [dict(r) for r in fx["schedule"]]
    lagging = next(r for r in sched if int(r["week"]) == 3)
    lagging["completed"] = "FALSE"
    today = "2026-09-26"

    def run(drop=None):
        cfb.allow_cfbd_calls(10)
        stub = _CFBDStub(fx, drop)
        saved_final = cfb.final_regular_weeks
        cfb.final_regular_weeks = lambda rows, today_=None: saved_final(rows, today)
        try:
            _, _, cache = _with_env(lambda: cfb.fetch_team_form_data(stub, 2026, [1, 2, 3], sched, {}))
        finally:
            cfb.final_regular_weeks = saved_final
            cfb.reset_cfbd_budget()
        return cache

    check("the edit really makes week 3 non-final by the flag",
          3 not in cfb.final_regular_weeks(sched, today), sorted(cfb.final_regular_weeks(sched, today)))
    cache = run()
    check("a week whose CFBD data covers every game is cached despite the lagging flag",
          "3" in cache["ppa"]["2026"] and "3" in cache["games_teams"]["2026"], sorted(cache["ppa"]["2026"]))
    gid = str(lagging["game_id"])
    cache = run(drop=(gid, lagging["home_team"]))
    check("but one team's missing PPA row keeps the week out of the cache (never cached with a game missing)",
          "3" not in cache["ppa"]["2026"], sorted(cache["ppa"]["2026"]))


def main():
    for fn in (test_a_week_is_cached_when_its_data_is_complete,
               test_the_week_cache_fetch_keeps_the_count,
               test_the_month_starts_from_the_measured_seed,
               test_the_run_is_capped_by_what_the_month_has_left,
               test_the_count_is_recorded_and_bounded,
               test_the_count_survives_the_early_returns):
        fn()
    cfb.reset_cfbd_budget()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("cfbd budget: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
