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
