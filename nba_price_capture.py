"""NBA price capture: append what ESPN quotes before tip, and nothing else.

    python3 nba_price_capture.py            # tonight's and tomorrow's games, appends
    python3 nba_price_capture.py --dry-run  # fetch and report, write nothing

WHY THIS EXISTS. NBA prices are LIVE-ONLY. ESPN strips a game's odds block
when it completes (0 of 12 completed games on 2026-10-28, 0 of 170 across
February 2026), and nothing in this repo stored one: builders run only for
sports in `active_game_sports`, and NBA is in neither gate. Measured
2026-11-04, 121 counted games played since 10-20 were already unrecoverable,
at about 8 a day (~56 a week). No NBA edge can be measured, at any price,
until a history exists -- and it only exists from the day something starts
keeping it. The owner approved a capture-only workflow on 2026-11-08; this is
it (built 2026-11-10).

CAPTURE ONLY. This module reads the scoreboard and appends to
data/nba_prices.jsonl. It imports no builder and no grader, touches neither
gate, never opens data/insights.games.json or the ledger, and nothing on the
site reads its file. NBA stays unregistered. It is also NOT a model input:
invariant 1 (price-blind scores) is untouched, because no score exists for
NBA and nothing here feeds one. Prices will enter at selection and
validation, which is what this file is the substrate for.

THE FILE IS APPEND-ONLY (invariant 6), and holds two row types:

  {"type": "price", ...}  one per CHANGE in a game's quoted numbers, plus the
                          first sighting. The first row for an event is its
                          first capture (the only honest CLV anchor NBA will
                          ever have); the last row before tip is its close.
  {"type": "run", ...}    one per run that saw at least one priced pre-game
                          event, listing every event id it saw priced.

  WHY BOTH. Writing every observation would store the same number a dozen
  times a day. Writing only changes loses something that matters: a game whose
  last price row is six hours before tip is either a line that did not move
  for six hours, or six hours of dropped scheduler runs -- and GitHub's
  scheduler is 3-9h late and drops whole days (CLAUDE.md). Those two cases
  give a "closing" price of very different quality and are indistinguishable
  from price rows alone. The run row is what separates them: an event id in a
  run's `priced_ids` with no new price row means "seen, unchanged, at this
  time". A run that saw nothing priced writes nothing, so quiet hours and the
  whole offseason cost no commits.

RAW FACTS, NOT A FILTERED VIEW. Every pre-game event carrying an odds block
is kept, with its season type and competition type stored beside it and the
counted-game rule's verdict at capture time (`counted`). The rule is NOT used
to drop rows, for one measured reason: playoff and play-in competition types
were never captured (open item, due before mid-April 2027), so a filter
written today would silently discard exactly the games nobody has seen yet.
A reader applies fetchers.nba.is_counted_game -- or its successor -- to the
stored fields, the same way the ledger keeps `observed` so grading rules can
be re-applied.

PRE-GAME ONLY, by status and not by the clock. A row is written only while
status.type.state == "pre". The clock is deliberately not consulted: a tip
that slips ten minutes is still pre-game and that late quote is the best
close there is, while a game that has started is "in" whatever the schedule
said. ESPN has never been observed carrying an NBA odds block on a started
game (the fixture's completed games have none), so the guard has not been
seen to bite on real data; it is there because a live in-game line stored as
a closing price would be wrong without looking wrong.

OFF IS KEPT. A block whose moneyline reads "OFF" is a book refusing to quote,
not missing data (espn_odds.MONEYLINE_OFF, the 2026-09-19 CFB lesson), so it
is stored with moneyline_off true and null prices rather than skipped.

NEVER A HALF-WRITTEN OR REWRITTEN FILE. Rows are appended in one write. An
unparseable existing line is reported and stepped over for change detection,
never repaired or dropped: stopping would lose prices every day the bad line
sat there, and rewriting would break the one property the file has. If every
scoreboard request fails the run exits 1 and writes nothing; if some of the
days fail, the rest are still captured and the run row names the days that
failed.

CADENCE IS NOT YET MEASURED. See .github/workflows/capture-nba-prices.yml.
The run rows (arrival times) and price rows (how often a line actually
moves, how early it first appears) are the measurement that will set it.

Sandbox note: site.api.espn.com is unreachable from the build sandbox
(2026-11-10, proxy 403), as it was for the session that built fetchers/nba.
Everything here is pinned offline against tools/verify/nba_games_fixture.json
(real captures, 2026-10-30/31); the first scheduled run is the first live one.
"""

import argparse
import datetime
import json
import os
import sys

import requests

import espn_odds
import slate_clock
from fetchers import nba

PRICES_PATH = "data/nba_prices.jsonl"
SCHEMA_VERSION = 1

# WHICH DAYS ARE ASKED FOR: yesterday, today and tomorrow (Eastern), one
# single-day request each, and NO filtering on the day afterwards.
#
# Yesterday is not a mistake. ESPN's `dates=YYYYMMDD` day boundary has never
# been measured for the NBA (CLAUDE.md: nba_grading.fetch_slate walks months
# "rather than trusting ESPN's unmeasured dates= day boundary"), and most games
# tip after 8 PM Eastern and so carry the NEXT day's UTC date. If the boundary
# is UTC, tonight's late games are filed under tomorrow; if it is Eastern or
# Pacific, they are under today. Three consecutive days cover tonight and
# tomorrow under any of those, and since rows are kept on status == "pre" and
# an odds block alone, an extra day costs one request and can add no wrong row.
#
# Tomorrow, because it is sometimes priced: a day ahead carried prices for 10
# of 10 games on 2026-10-29 and 0 of 4 on 10-31; two days ahead, 0 of 15
# (fetchers/nba docstring). Further out has not been seen priced at all.
DAYS_BEFORE = 1
DAYS_AHEAD = 1

# What makes a new price row. captured_at is excluded on purpose (it differs
# every run), and so are the identity fields, which cannot change for an id.
# The OPEN numbers are included: ESPN publishes them as fixed, and a row that
# shows one moving is worth having precisely because it should not happen.
PRICE_FIELDS = ("provider", "home_ml", "away_ml", "moneyline_off",
                "home_ml_open", "away_ml_open", "spread", "spread_open",
                "spread_close", "total", "details")


def _utc_stamp(now):
    return now.astimezone(datetime.timezone.utc).replace(microsecond=0) \
              .isoformat().replace("+00:00", "Z")


def _state(event):
    return (((nba._comp(event).get("status") or {}).get("type") or {}).get("state"))


def observe(events, now):
    """Every pre-game event in `events` that carries an odds block, as a price
    row stamped `now`. No change detection here: this is what the feed says."""
    stamp = _utc_stamp(now)
    rows = []
    for event in events:
        if _state(event) != "pre":
            continue                       # see PRE-GAME ONLY in the docstring
        block = espn_odds.parse_event(event)
        if block is None:
            continue                       # no market published yet: intermittent, normal
        comp = nba._comp(event)
        sides = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation")
                 for c in comp.get("competitors") or []}
        row = {
            "type": "price",
            "schema_version": SCHEMA_VERSION,
            "event_id": str(event.get("id")),
            "slate_date": nba.slate_date(event),     # Eastern (invariant 3)
            "start": event.get("date"),
            "home": sides.get("home"),
            "away": sides.get("away"),
            # Raw classification, so the counted-game rule can be re-applied
            # (or replaced, once playoff types are known) to rows already kept.
            "season_type": (event.get("season") or {}).get("type"),
            "season_year": (event.get("season") or {}).get("year"),
            "comp_type_id": str((comp.get("type") or {}).get("id")),
            "counted": nba.is_counted_game(event),
        }
        for field in PRICE_FIELDS:
            row[field] = block.get(field)
        row["captured_at"] = stamp
        rows.append(row)
    return rows


def read_rows(path):
    """(rows, bad_line_numbers) from the capture file; ([], []) if absent.

    A line that will not parse is counted and stepped over, never fatal and
    never repaired -- see NEVER A HALF-WRITTEN OR REWRITTEN FILE."""
    rows, bad = [], []
    if not os.path.exists(path):
        return rows, bad
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad.append(number)
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad.append(number)
    return rows, bad


def latest_prices(rows):
    """{event_id: its most recent price row}, in file order."""
    out = {}
    for row in rows:
        if row.get("type") == "price" and row.get("event_id"):
            out[str(row["event_id"])] = row
    return out


def _quote(row):
    return tuple(row.get(field) for field in PRICE_FIELDS)


def plan(observations, existing_rows, now, days, failed_days, events_seen):
    """The rows this run should append: one price row per event whose quote is
    new or changed, then one run row if anything was seen priced. Returns []
    when nothing was priced, so an idle run leaves the file byte-identical."""
    known = latest_prices(existing_rows)
    new_rows = []
    for obs in observations:
        before = known.get(obs["event_id"])
        if before is None or _quote(before) != _quote(obs):
            new_rows.append(obs)
            known[obs["event_id"]] = obs   # two listings of one id in a run: keep one
    if not observations:
        return []
    new_rows.append({
        "type": "run",
        "schema_version": SCHEMA_VERSION,
        "captured_at": _utc_stamp(now),
        "days": list(days),
        "failed_days": list(failed_days),
        "events": events_seen,
        "priced_ids": sorted({obs["event_id"] for obs in observations}),
        "new_price_rows": len(new_rows),
    })
    return new_rows


def append_rows(path, rows):
    """Append `rows` in a single write. Existing bytes are never touched; if
    the file does not end in a newline (a cut-off earlier write), one is added
    first so the new rows cannot fuse onto a partial line."""
    if not rows:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lead = ""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                lead = "\n"
    payload = lead + "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(payload)


def slate_days(now=None):
    """The days to request, Eastern, as YYYY-MM-DD: see DAYS_BEFORE."""
    if now is None:
        today = slate_clock.eastern_now().date()
    else:
        today = datetime.date.fromisoformat(slate_clock.eastern_date(now))
    return [(today + datetime.timedelta(days=n)).isoformat()
            for n in range(-DAYS_BEFORE, DAYS_AHEAD + 1)]


def fetch_days(session, days):
    """(events, failed_days). One SINGLE-DAY request per day (ESPN answers 400
    to a range -- invariant 11), through fetchers.nba.get_events and so through
    espn_dates.checked_events and its limit=500. An event listed under two
    days is kept once."""
    events, failed, seen = [], [], set()
    for day in days:
        try:
            got = nba.get_events(session, {"dates": day.replace("-", "")})
        except Exception as exc:  # noqa: BLE001 -- one day down must not cost the other
            print("nba-prices: {} failed ({}: {})".format(
                day, type(exc).__name__, str(exc)[:160]), file=sys.stderr)
            failed.append(day)
            continue
        for event in got:
            key = str(event.get("id"))
            if key not in seen:
                seen.add(key)
                events.append(event)
    return events, failed


def run(session, path=PRICES_PATH, now=None, dry_run=False):
    """One capture. Exit code 0, or 1 when no day could be fetched at all."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    days = slate_days(now)
    events, failed = fetch_days(session, days)
    if len(failed) == len(days):
        print("nba-prices: every scoreboard request failed; nothing written",
              file=sys.stderr)
        return 1
    existing, bad = read_rows(path)
    if bad:
        print("nba-prices: WARNING {} unparseable line(s) in {} (line {}); left "
              "in place and skipped for change detection".format(
                  len(bad), path, ", ".join(map(str, bad[:5]))), file=sys.stderr)
    observations = observe(events, now)
    rows = plan(observations, existing, now, days, failed, len(events))
    pre = sum(1 for e in events if _state(e) == "pre")
    print("nba-prices: {} -- {} events, {} pre-game, {} priced, {} new price row(s){}"
          .format(", ".join(days), len(events), pre, len(observations),
                  sum(1 for r in rows if r["type"] == "price"),
                  "; FAILED " + ", ".join(failed) if failed else ""))
    if not dry_run:
        append_rows(path, rows)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", default=PRICES_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    args = parser.parse_args(argv)
    return run(requests.Session(), path=args.path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
