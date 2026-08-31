"""THE SLATE BOUNDARY: one definition of "which day's games is this?".

WHY THIS FILE EXISTS. It did not, and the two halves of the pipeline disagreed.
generate_insights stamped a store with `generated_at.date()` -- a UTC date,
because generate_stats builds `generated_at` as datetime.now(timezone.utc) --
while signal_report asked for yesterday in US/Eastern. Those agree only for a
run landing between about 04:00 and 23:59 UTC, which the workflow's 13:40 and
15:40 cron entries comfortably do.

Then GitHub started firing them nine to eleven hours late, and both landed
either side of UTC midnight. On 2026-08-27 the 23:02Z run and the 00:34Z run
computed the SAME `yesterday_et` (2026-08-26, correctly, since both were still
2026-08-27 in Eastern) -- so no run ever asked about 2026-08-27 -- while the
second one rolled the store forward to a UTC date of 2026-08-28. By the time a
run did ask for 2026-08-27, its pre-game snapshot was gone. Two days of MLB
picks (2026-08-27 and 2026-08-28) went ungraded, and the ledger recorded them
as `no_store` gaps that no replay can honestly fill.

EASTERN IS THE RIGHT BOUNDARY, and not by convention. It is the boundary the
leagues themselves use: a game at 10pm Eastern on the 28th is on the 28th's
slate even though it is already the 29th in UTC, and MLB's StatsAPI returns it
under date=2026-08-28. It is also what the site displays (`start` is rendered
in ET) and what signal_report has always graded against.

So the rule is: ANYTHING that names a slate uses this module. A caller that
wants "the date whose games these are" must not reach for .date() on a UTC
timestamp, however obvious that looks.
"""

import datetime

# Fixed-offset fallback for a host with no tzdata. EDT, not EST, because the
# only thing this changes is which side of midnight a late-evening run lands
# on, and the seasons this repo covers (MLB, EPL, CFB) run overwhelmingly
# through the daylight-saving half of the year. Getting it wrong shifts a
# result by an hour, never by a day, for any run outside 23:00-01:00 UTC.
_FALLBACK = datetime.timezone(datetime.timedelta(hours=-4))
_ZONE = "America/New_York"


def eastern_now():
    """Now, in US/Eastern."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(_ZONE))
    except Exception:  # no tzdata available
        return datetime.datetime.now(_FALLBACK)


def eastern_date(when=None):
    """The SLATE DATE for `when` (a tz-aware datetime), as YYYY-MM-DD.

    Pass the run's `generated_at` and get the date whose games it is building.
    Passing a naive datetime is a caller bug and raises rather than guessing a
    zone -- a silent guess here is exactly the class of error this module was
    written to end.
    """
    if when is None:
        return eastern_now().date().isoformat()
    if when.tzinfo is None:
        raise ValueError(
            "slate_clock.eastern_date needs a timezone-aware datetime; got a naive "
            "one, which cannot be converted without inventing a zone")
    try:
        from zoneinfo import ZoneInfo
        return when.astimezone(ZoneInfo(_ZONE)).date().isoformat()
    except Exception:  # no tzdata available
        return when.astimezone(_FALLBACK).date().isoformat()


def yesterday():
    """The slate date one day back -- what a grading run means by "yesterday"."""
    return (eastern_now().date() - datetime.timedelta(days=1)).isoformat()


# How far past an empty fixture window a slate may be pulled forward from.
#
# Two weeks, and both bounds are deliberate. Short enough that a genuine
# offseason still shows an EMPTY tab -- "nothing on" is information, and a
# college football Games tab quietly displaying September's opener all through
# March would be worse than a blank one. Long enough to cover every real
# in-season gap: an NFL preseason tail (nine days on the 2026 calendar), an EPL
# international break, the CFB midweek desert, a bye.
SLATE_LOOKAHEAD_DAYS = 14


def window_start(available_dates, start, window_days,
                 lookahead_days=SLATE_LOOKAHEAD_DAYS):
    """Where a fixture window should actually begin.

    Normally `start` -- today. But a window tuned for a sport's usual cadence
    goes blank in any gap longer than itself, and then the tab shows nothing
    while the fixtures it would show are sitting in the schedule already,
    fully scoreable. That is what happened to NFL: the season opened nine days
    out, the window reached seven, and week 1 -- the most anticipated slate of
    the year, whose picks come from last season's margin and could not change
    between now and kickoff -- rendered as an empty tab for two days.

    So: if nothing falls inside [start, start + window_days], jump to the
    NEXT date that has fixtures and let the caller window from there. The
    caller keeps its own window length, so this shows the next SLATE rather
    than a single next game -- pulling NFL forward to 2026-09-09 picks up the
    whole of week 1 (Wed, Thu, Sun, Mon), not just the Wednesday opener.

    Returns `start` unchanged when the window already has fixtures, and also
    when the next one is further off than `lookahead_days` -- see
    SLATE_LOOKAHEAD_DAYS for why an offseason must stay visibly empty.

    `available_dates` is any iterable of YYYY-MM-DD strings; unparseable and
    empty entries are ignored rather than raising, since they come from feeds.
    """
    try:
        begin = datetime.date.fromisoformat(start)
    except (TypeError, ValueError):
        return start
    window_end = begin + datetime.timedelta(days=window_days)
    horizon = begin + datetime.timedelta(days=lookahead_days)
    upcoming = []
    for raw in available_dates or ():
        try:
            day = datetime.date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        if day >= begin:
            upcoming.append(day)
    if not upcoming:
        return start
    if any(day <= window_end for day in upcoming):
        return start                      # the window already has fixtures
    nxt = min(upcoming)
    return nxt.isoformat() if nxt <= horizon else start
