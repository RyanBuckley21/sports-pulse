#!/usr/bin/env python3
"""Season-long backtest of the MLB Signal Score against real outcomes.

    python3 backtest_season.py --start 2026-08-01 --end 2026-08-07   # dry run
    python3 backtest_season.py                                       # full season

Standalone: does not modify signal_report.py's live grading path,
daily-stats-and-grade.yml, betting_signals.py, implied_total.py, or
config.yaml. Reuses them read-only:

  * fetchers.mlb.build_game_entities -- the SAME builder generate_stats.py's
    pipeline calls every day (schedule -> team/pitcher form -> betting_signals
    scoring -> standout selection) -- run fresh, in memory, for a past date.
    Never touches the committed data/insights.games.json or data/boxscores.json;
    this script keeps its own boxscore cache for the lifetime of one run only.
  * signal_report.collect_picks / grade / build_pick_rows / build_status_row --
    the SAME pick-selection and grading logic the live ledger uses, imported
    and called as-is. Nothing here re-derives a verdict rule.

Results go to data/backtest_2026.jsonl -- NEVER data/signal_report_history.jsonl,
the live production ledger. Same row schema, plus "backtest": true so a reader
can never mistake one file's rows for the other's.

Per-game failures are already isolated inside build_game_entities (a bad game
is logged and skipped; the rest of the slate still builds -- see its
docstring). This script adds the next layer up: a bad DATE (every game
failed, a network error, a truly malformed slate) is logged and skipped so
one bad day can't abort a multi-month run.
"""

import argparse
import datetime
import sys
import time

import requests

import signal_report
from fetchers import mlb

OUTPUT_PATH = "data/backtest_2026.jsonl"
SPORT_KEY = "mlb"
SOURCE = "backtest:fetchers.mlb.build_game_entities"

DEFAULT_START = "2026-03-25"  # 2026 MLB regular-season opener (gameType=R)


def daterange(start, end):
    d = datetime.date.fromisoformat(start)
    stop = datetime.date.fromisoformat(end)
    while d <= stop:
        yield d.isoformat()
        d += datetime.timedelta(days=1)


def most_recent_completed_date(session, base_url, start_from=None):
    """Walk backward from `start_from` (default: yesterday US/Eastern) until
    every game on a date is either Final or called off -- i.e. a slate this
    script can fully grade, not one still in progress. Gives up after 7 days
    and returns `start_from` anyway (a schedule fetch failure shouldn't block
    the caller from getting a default at all)."""
    d = start_from or signal_report.yesterday_et()
    for _ in range(7):
        try:
            slate = signal_report.fetch_slate(session, base_url, d)
        except requests.RequestException:
            return d
        if slate and all(signal_report.is_final(g) or signal_report.is_called_off(g)
                          for g in slate.values()):
            return d
        d = (datetime.date.fromisoformat(d) - datetime.timedelta(days=1)).isoformat()
    return d


def backtest_date(date, config, session, base_url, boxscore_cache, min_score):
    """Build the day's store fresh and grade it exactly as signal_report.py
    would. Returns (rows, boxscore_cache, status), where `rows` is a list of
    (pick, game, result, verdict, basis) tuples and `status` is None (rows are
    real picks), "no_games" (nothing scheduled -- an off day), or "no_picks"
    (games happened, nothing cleared the score floor)."""
    entities, boxscore_cache, _training_rows = mlb.build_game_entities(
        config, date, boxscore_cache, team_entities=None)
    if not entities:
        return [], boxscore_cache, "no_games"

    picks = signal_report.collect_picks(entities, config, min_score, all_markets=True)
    if not picks:
        return [], boxscore_cache, "no_picks"

    slate = signal_report.fetch_slate(session, base_url, date)
    called_off = {p["gamePk"] for p in picks
                  if p["gamePk"] in slate and signal_report.is_called_off(slate[p["gamePk"]])}
    replays = {}
    if called_off:
        try:
            replays = signal_report.fetch_replay_dates(session, base_url, called_off)
        except requests.RequestException:
            replays = {}  # best-effort; the row still reports the postponement

    rows = []
    for pick in picks:
        game = slate.get(pick["gamePk"])
        pick["replayed_on"] = replays.get(pick["gamePk"])
        result, verdict, basis = signal_report.grade(pick, game, assume_lines=False)
        rows.append((pick, game, result, verdict, basis))
    return rows, boxscore_cache, None


def tag_backtest(rows):
    for row in rows:
        row["backtest"] = True
    return rows


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default=DEFAULT_START, metavar="YYYY-MM-DD",
                   help="first date to backtest (default: {}, the 2026 opener)".format(DEFAULT_START))
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="last date to backtest (default: the most recent fully-completed slate)")
    p.add_argument("--min-score", type=int, default=None, metavar="N",
                   help="Signal Score floor (default: betting_signals.<sport>.standout_threshold)")
    p.add_argument("--out", default=OUTPUT_PATH, help="output JSONL path (default: {})".format(OUTPUT_PATH))
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = signal_report.load_config()
    base_url = config[SPORT_KEY]["base_url"]
    threshold = (config.get("betting_signals") or {}).get(SPORT_KEY, {}).get("standout_threshold", 50)
    min_score = args.min_score if args.min_score is not None else threshold

    session = requests.Session()
    end = args.end or most_recent_completed_date(session, base_url)
    dates = list(daterange(args.start, end))
    # Each invocation is a complete, self-contained replay of its date range --
    # not an append-forever log like the live ledger, which has to accumulate
    # across separate daily runs. Truncating here means re-running (e.g. the
    # full season after a one-week dry run over the same start date) never
    # leaves stale or duplicate rows from an earlier partial run sitting
    # alongside the new ones.
    open(args.out, "w").close()
    print("backtest_season: {} dates, {} through {}, min_score={}, writing to {} (truncated)"
          .format(len(dates), args.start, end, min_score, args.out))

    run_id = signal_report._now_iso()
    boxscore_cache = {}
    totals = {"HIT": 0, "MISS": 0, "PUSH": 0}
    n_picks = n_recorded_dates = n_no_picks = n_no_games = n_skipped = 0
    t_start = time.time()

    for i, date in enumerate(dates, 1):
        try:
            rows, boxscore_cache, status = backtest_date(
                date, config, session, base_url, boxscore_cache, min_score)
        except mlb.SlateBuildError as e:
            print("backtest_season: {}: SLATE BUILD FAILED ({}); skipped".format(date, e))
            n_skipped += 1
            continue
        except requests.RequestException as e:
            print("backtest_season: {}: network error ({}); skipped".format(date, e))
            n_skipped += 1
            continue
        except Exception as e:  # noqa: BLE001 -- one bad date must not cost the run
            print("backtest_season: {}: unexpected {} ({}); skipped"
                  .format(date, type(e).__name__, str(e)[:160]))
            n_skipped += 1
            continue

        if status == "no_games":
            print("backtest_season: {}: no games scheduled -- off day".format(date))
            n_no_games += 1
            continue
        if status == "no_picks":
            signal_report.append_ledger(
                tag_backtest([signal_report.build_status_row(
                    date, signal_report.STATUS_NO_PICKS, SOURCE, run_id)]),
                path=args.out)
            print("backtest_season: {}: 0 picks cleared the bar".format(date))
            n_no_picks += 1
            continue

        day_counts = {"HIT": 0, "MISS": 0, "PUSH": 0}
        for _, _, _, verdict, basis in rows:
            if basis in ("outcome", "estimate") and verdict in day_counts:
                day_counts[verdict] += 1
                totals[verdict] += 1
        pick_rows = tag_backtest(signal_report.build_pick_rows(date, rows, SOURCE, run_id))
        signal_report.append_ledger(pick_rows, path=args.out)
        n_picks += len(rows)
        n_recorded_dates += 1
        print("backtest_season: {}: {} picks -- {} HIT / {} MISS / {} PUSH ({}/{} dates, {:.0f}s elapsed)"
              .format(date, len(rows), day_counts["HIT"], day_counts["MISS"], day_counts["PUSH"],
                      i, len(dates), time.time() - t_start))

    graded = totals["HIT"] + totals["MISS"]
    print("\nbacktest_season: done in {:.0f}s -- {} dates graded, {} no-pick, {} off days, {} skipped"
          .format(time.time() - t_start, n_recorded_dates, n_no_picks, n_no_games, n_skipped))
    print("backtest_season: {} total picks -- {}-{}{} (outcome+estimate combined)".format(
        n_picks, totals["HIT"], totals["MISS"],
        " ({:.1f}%)".format(100.0 * totals["HIT"] / graded) if graded else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
