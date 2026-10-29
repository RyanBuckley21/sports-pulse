"""ESPN scoreboard date windows, after ranges stopped working.

WHAT CHANGED. Every scoreboard fetcher in this repo asked for a span the
obvious way -- `dates=YYYYMMDD-YYYYMMDD` -- and on 2026-09-15 ESPN began
answering HTTP 400 to all of them. Measured against the live feed rather than
inferred:

    soccer/eng.1              dates=20260918-20260924   -> 400
    football/college-football dates=20260918-20260924   -> 400
    football/nfl              dates=20260918-20260924   -> 400
    any league                dates=20260920            -> 200
    any league                dates=202609              -> 200

Width is not the issue, and neither is `limit`: 60, 120, 200, 250, 300, 320,
330, 340 and 360 days all 400, at every limit and with none. A single day and
a whole month both still work. Ranges are simply gone.

WHY IT WENT UNNOTICED FOR NINE DAYS. Only one of the six call sites ran on
every build. EPL's season fetch raised, generate_insights froze that partition
rather than clearing it -- the store-protection rule working exactly as
designed, since an exception means "unknown" rather than "no games" -- and the
Games tab sat on one stale fixture with nothing on screen to say so. The other
five were latent: two ESPN fallbacks that only run when the primary source is
late, and three replay lookups that only run when a game is postponed. They
would have failed the first time they were needed, which is the worst time.

SO THE WINDOW IS BUILT FROM MONTHS and filtered locally. One request per month
touched -- usually one, two across a month end -- and the caller narrows to the
days it actually wanted, because a month query returns the whole month.
"""

import datetime

# THE LARGEST `limit` ESPN HONOURS, and a silent cap above it. Measured
# 2026-09-27 on football/college-football, groups=80, dates=20260926 (a
# Saturday with 65 FBS events): limit 100, 200, 300, 400 and 500 all return
# 65; limit 501, 999 and 1000 return exactly 25. HTTP 200 every time, no
# pageCount, nothing to say it was cut. With NO limit the month of 202609
# returns 300 of its 323 events -- a second, quieter default cap. soccer/eng.1
# and football/nfl honour 1000 today, but nothing promises they will, and no
# month in either league comes near 500, so every caller uses this one value.
#
# What it cost before it was found: cfb_grading asked for 1000, so it saw 25
# of each Saturday's games and DEFERRED every pick outside them as "not on
# this date's schedule" -- 12 of 17 on 2026-09-26, never graded, since the
# next run grades the next date. The fallback schedule asked for 1000 per
# month too. Nothing threw.
SCOREBOARD_LIMIT = 500


def checked_events(payload, limit=SCOREBOARD_LIMIT, what="ESPN scoreboard"):
    """The payload's events, or RuntimeError when there are as many as were
    asked for. A full page means the answer may be cut short, and a caller
    that grades or builds from a cut list writes confident wrong rows (see
    SCOREBOARD_LIMIT). Raising turns that into a failed step, which the
    pipeline already knows how to survive: a grading step is re-run, a
    builder's partition is frozen rather than overwritten."""
    events = (payload or {}).get("events") or []
    if len(events) >= limit:
        raise RuntimeError("{}: {} events at limit={} -- the response may be truncated; "
                           "refusing to use a possibly partial list".format(what, len(events), limit))
    return events


# Hard ceiling on any month walk, so a bad range or a feed that never reports a
# boundary cannot spin. Fourteen: an EPL season spans ten months of fixtures,
# and a full Aug->Jul prior-season fetch is twelve keys.
MAX_MONTHS = 14


def month_key(day):
    """A date -> the YYYYMM string ESPN accepts."""
    return day.strftime("%Y%m")


def prev_month(day):
    """The last day of the month before `day`.

    Via the first of the month minus one day rather than a 30-day step, which
    lands wrong on February and on any 31st."""
    return day.replace(day=1) - datetime.timedelta(days=1)


def month_keys(start, end, max_months=MAX_MONTHS):
    """The YYYYMM keys covering [start, end] inclusive, oldest first.

    Callers MUST still filter the events they get back to the days they asked
    for: a month query returns the whole month, including fixtures outside the
    window and days already played."""
    if end < start:
        return []
    keys, cursor, last = [], start.replace(day=1), end.replace(day=1)
    while cursor <= last and len(keys) < max_months:
        keys.append(month_key(cursor))
        # +32 days from the 1st always lands in the next month, whatever its
        # length; snapping back to the 1st keeps the walk on month boundaries.
        cursor = (cursor + datetime.timedelta(days=32)).replace(day=1)
    return keys


def compact(day):
    """A date -> the YYYYMMDD string these call sites pass around."""
    return day.strftime("%Y%m%d")


def from_compact(value):
    """YYYYMMDD -> date. These strings come from callers, not from the feed."""
    text = str(value)[:8]
    return datetime.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def in_window(event, lo_compact, hi_compact):
    """Whether a scoreboard event's date falls inside [lo, hi], both YYYYMMDD.

    ESPN dates arrive as ISO timestamps ('2026-09-20T14:00Z'); this compares
    them in the compact form the callers already hold. An event with no usable
    date is EXCLUDED -- a window filter that silently admits undated rows is
    how a month's worth of extra fixtures reaches a slate."""
    stamp = (event.get("date") or "").replace("-", "")[:8]
    return bool(stamp) and lo_compact <= stamp <= hi_compact


def fetch_window(get_json, start, end, params=None, max_months=MAX_MONTHS):
    """Every event in [start, end], gathered month by month and filtered.

    `get_json(params) -> payload` is supplied by the caller, so this module
    stays out of the HTTP business entirely: each call site already has its own
    session, URL, timeout, auth and error policy, and this must not quietly
    impose a different one.
    """
    lo, hi = compact(start), compact(end)
    out = []
    for key in month_keys(start, end, max_months):
        payload = get_json(dict(params or {}, dates=key)) or {}
        out.extend(e for e in (payload.get("events") or []) if in_window(e, lo, hi))
    return out
