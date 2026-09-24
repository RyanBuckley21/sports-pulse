"""EPL's scoreboard fetch, after ESPN stopped serving date ranges.

WHAT HAPPENED. This fetcher asked for a whole season in one request --
`dates=20250929-20260920` -- and on 2026-09-15 ESPN began answering HTTP 400.
Not a width cap, which is what the old comment in fetchers/epl.py guessed when
it picked a 360-day lookback to duck one: measured against the live feed, 60,
120, 200, 250, 300, 320, 330, 340 and 360 days ALL 400, at every `limit` and
with none, while a single `dates=YYYYMMDD` and a month `dates=YYYYMM` both
still return 200. Ranges are simply gone.

WHY IT WAS SILENT FOR NINE DAYS. The fetch raised, generate_insights froze the
EPL partition rather than clearing it -- the store-protection rule working
exactly as designed, since an exception means "unknown", not "no games" -- and
the Games tab sat on one stale fixture from 2026-09-15 with nothing on screen
saying so. Every test in the suite stayed green throughout, because nothing
asserted the SHAPE of the request.

So the guard that matters most here is the cheapest one: no call this module
makes may ever ask for a range again. It would have caught the outage the day
ESPN changed, and it is the one check that cannot go stale.

NO NETWORK. Every response below is a fake session replaying trimmed real
payloads.
"""

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import espn_dates  # noqa: E402
from fetchers import epl  # noqa: E402

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"


def event(date, season, home="Arsenal", away="Chelsea", hs=1, as_=0, completed=True):
    return {"id": date + home, "date": date + "T14:00Z", "season": {"year": season},
            "competitions": [{"status": {"type": {"completed": completed}},
                              "competitors": [
                                  {"homeAway": "home", "score": str(hs),
                                   "team": {"id": "1", "displayName": home}},
                                  {"homeAway": "away", "score": str(as_),
                                   "team": {"id": "2", "displayName": away}}]}]}


class FakeSession(object):
    """Replays {month key: [events]} and records every `dates` asked for."""

    def __init__(self, months):
        self.months, self.asked = months, []


def install(months):
    sess = FakeSession(months)

    def fake_get(session, url, params=None, **kw):
        key = (params or {}).get("dates")
        sess.asked.append(key)
        return {"events": list(sess.months.get(key, []))}

    epl._get = fake_get
    return sess


_real_get = epl._get


# ----------------------------------------------------------- month helpers
ok("a month key is YYYYMM", espn_dates.month_key(datetime.date(2026, 9, 24)) == "202609")
ok("the previous month of a 1st is the month before",
   espn_dates.month_key(espn_dates.prev_month(datetime.date(2026, 3, 1))) == "202602")
ok("  and of a 31st too",
   espn_dates.month_key(espn_dates.prev_month(datetime.date(2026, 3, 31))) == "202602")
ok("January steps back to December of the previous year",
   espn_dates.month_key(espn_dates.prev_month(datetime.date(2026, 1, 15))) == "202512")
# Leap-year February is where a naive 30-day step breaks.
ok("March steps back to February in a leap year",
   espn_dates.month_key(espn_dates.prev_month(datetime.date(2024, 3, 5))) == "202402")

ok("a span inside one month is one key",
   espn_dates.month_keys(datetime.date(2026, 9, 3), datetime.date(2026, 9, 24)) == ["202609"])
ok("a span crossing a month end is two",
   espn_dates.month_keys(datetime.date(2026, 9, 24), datetime.date(2026, 10, 7))
   == ["202609", "202610"])
ok("  and one crossing a year end is right",
   espn_dates.month_keys(datetime.date(2026, 12, 28), datetime.date(2027, 1, 9))
   == ["202612", "202701"])
ok("a span is bounded", len(espn_dates.month_keys(datetime.date(2020, 1, 1),
                                            datetime.date(2026, 1, 1))) <= espn_dates.MAX_MONTHS)


# --------------------------------------------- THE REGRESSION GUARD
# The one check that would have caught the outage on the day it happened.
try:
    sess = install({"202609": [event("2026-09-20", 2026)],
                    "202608": [event("2026-08-21", 2026)],
                    "202607": []})
    epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24))
    ok("the season fetch asks for months, never a range",
       all(re.fullmatch(r"\d{6}", str(d)) for d in sess.asked), sess.asked)
    ok("  and asks for at least one", len(sess.asked) >= 1, sess.asked)
    ok("  never with a '-' in the dates parameter",
       not any("-" in str(d) for d in sess.asked), sess.asked)
finally:
    epl._get = _real_get


# ------------------------------------------------------ the backward walk
try:
    sess = install({
        "202609": [event("2026-09-20", 2026), event("2026-09-12", 2026)],
        "202608": [event("2026-08-21", 2026)],
        "202607": [],                                  # the summer gap
        "202606": [event("2026-06-02", 2025)],         # last season -- must not be reached
    })
    matches, season = epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24))
    ok("the walk stops at the summer gap", sess.asked == ["202609", "202608", "202607"], sess.asked)
    ok("  keeping only this season", season == 2026 and len(matches) == 3, (season, len(matches)))
    ok("  oldest first", [m["date"] for m in matches]
       == ["2026-08-21", "2026-09-12", "2026-09-20"], [m["date"] for m in matches])
finally:
    epl._get = _real_get

# THE AUGUST STRADDLE: a month can carry the tail of last season. A walk that
# stopped only on empty months would swallow it, and form must never cross that
# boundary -- three clubs go down and three come up every summer, so last
# season's table is a different league.
try:
    sess = install({
        "202609": [event("2026-09-20", 2026)],
        "202608": [event("2026-08-21", 2026)],
        "202607": [event("2026-07-04", 2025)],         # wholly last season
        "202606": [event("2026-06-02", 2025)],
    })
    matches, season = epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24))
    ok("a month wholly of the previous season ends the walk",
       sess.asked == ["202609", "202608", "202607"], sess.asked)
    ok("  and none of it is kept", season == 2026 and len(matches) == 2, (season, len(matches)))
finally:
    epl._get = _real_get

# A run inside the summer gap must still reach back to the season that ended,
# so neither stop rule may fire before the first month with any events.
try:
    sess = install({"202607": [], "202606": [], "202605": [event("2026-05-24", 2025)],
                    "202604": [event("2026-04-11", 2025)], "202603": []})
    matches, season = epl.get_season_matches(sess, URL, datetime.date(2026, 7, 20))
    ok("a summer run walks past empty months to the season that ended",
       season == 2025 and len(matches) == 2, (season, len(matches), sess.asked))
finally:
    epl._get = _real_get

# Bounded three ways, and the cap is real rather than decorative.
try:
    sess = install({})
    epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24))
    ok("an entirely empty feed still terminates",
       len(sess.asked) <= espn_dates.MAX_MONTHS, len(sess.asked))
    sess = install({espn_dates.month_key(datetime.date(2026, 9, 24) - datetime.timedelta(days=30 * i)):
                    [event("2026-09-20", 2026)] for i in range(20)})
    epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24))
    ok("  and a feed that never reports a boundary is capped",
       len(sess.asked) <= espn_dates.MAX_MONTHS, len(sess.asked))
    sess = install({"202609": [event("2026-09-20", 2026)], "202608": [event("2026-08-21", 2026)]})
    epl.get_season_matches(sess, URL, datetime.date(2026, 9, 24), lookback_days=10)
    ok("  and a short lookback stops it sooner", sess.asked == ["202609"], sess.asked)
finally:
    epl._get = _real_get


# ------------------------------------------------------------ config sanity
ok("the lookback is still about a season", 300 <= epl.SEASON_LOOKBACK_DAYS <= 400)
ok("the month cap covers a ten-month season with slack",
   11 <= espn_dates.MAX_MONTHS <= 20)

# ------------------------------------------------------- espn_dates itself
ok("an end before the start yields nothing",
   espn_dates.month_keys(datetime.date(2026, 9, 1), datetime.date(2026, 8, 1)) == [])
ok("a full Aug->Jul prior season is twelve keys, inside the cap",
   len(espn_dates.month_keys(datetime.date(2025, 8, 1), datetime.date(2026, 7, 31))) == 12)
ok("compact round-trips",
   espn_dates.compact(espn_dates.from_compact("20260924")) == "20260924")
ok("an event inside the window passes",
   espn_dates.in_window({"date": "2026-09-20T14:00Z"}, "20260918", "20260924"))
ok("  one outside does not",
   not espn_dates.in_window({"date": "2026-10-11T14:00Z"}, "20260918", "20260924"))
ok("  the bounds are inclusive",
   espn_dates.in_window({"date": "2026-09-18T00:00Z"}, "20260918", "20260924")
   and espn_dates.in_window({"date": "2026-09-24T23:00Z"}, "20260918", "20260924"))
# An undated row must be EXCLUDED: a window filter that silently admits them is
# how a month of extra fixtures reaches a slate.
ok("  an undated event is excluded, not admitted",
   not espn_dates.in_window({}, "20260918", "20260924")
   and not espn_dates.in_window({"date": None}, "20260918", "20260924"))

_calls = []


def _fake_get(params):
    _calls.append(params["dates"])
    return {"events": [event("2026-09-20", 2026), event("2026-09-12", 2026),
                       event("2026-10-11", 2026)]}


got = espn_dates.fetch_window(_fake_get, datetime.date(2026, 9, 18),
                              datetime.date(2026, 9, 24), params={"limit": 10})
ok("fetch_window asks month by month", _calls == ["202609"], _calls)
ok("  and narrows a whole month to the days asked for",
   [e["date"][:10] for e in got] == ["2026-09-20"], [e["date"][:10] for e in got])
_calls[:] = []
espn_dates.fetch_window(_fake_get, datetime.date(2026, 9, 24), datetime.date(2026, 10, 7))
ok("  crossing a month end takes two requests", _calls == ["202609", "202610"], _calls)


# NO RANGE ANYWHERE. There were SIX call sites across five files, and fixing
# only EPL's season fetch left the Games tab just as dead -- the fixture window
# was still asking for `dates=20260923-20261007`. Five of the six were LATENT:
# two ESPN fallbacks that run only when the primary source is late, and three
# replay lookups that run only when a game is postponed. Each would have failed
# the first time it was needed. A source sweep catches a seventh.
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _f in ("fetchers/epl.py", "fetchers/cfb.py", "fetchers/nfl.py",
           "cfb_grading.py", "epl_grading.py", "nfl_grading.py", "espn_odds.py"):
    _src = open(os.path.join(_root, _f)).read()
    ok("{} builds no range-shaped dates parameter".format(_f),
       '"{}-{}".format(' not in _src, _f)


print("epl fetch: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "epl fetch: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
