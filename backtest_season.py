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


# --------------------------------------------------------------------------- #
# Point-in-time team bullpen ERA: backtest-only, never touches fetchers/mlb.py
# --------------------------------------------------------------------------- #
#
# fetchers.mlb.team_season_bullpen_era queries the relief-pitcher ("rp")
# situational split, which the MLB Stats API only ever answers UNBOUNDED --
# "as of right now." That is exactly right for live generation, which only
# ever asks about today, and exactly wrong for a backtest asking about a past
# date: it silently includes relief outings that had not happened yet as of
# that date.
#
# There is no single-call fix. `byDateRange` (the stat type that DOES take a
# date range) does not honor `sitCodes` -- verified directly against the live
# API: `stats=byDateRange&sitCodes=rp` for a real team returns
# `gamesStarted=59`, the FULL pitching staff, not the reliever-only split's 0.
# The only mechanism that is genuinely point-in-time is boxscore
# reconstruction -- team_bullpen_era's own existing technique for its 7-day
# window, extended to a season-length one -- and that changes the NUMBER even
# for today: measured live for a real team, 3.13 ERA / 414.2 IP from the
# current statSplits call vs 3.38 / 594.7 reconstructed through yesterday,
# because GS=0 boxscore reconstruction and the official "rp" situational code
# simply disagree on what counts as bullpen work (a smaller version of this
# same gap is already documented in team_bullpen_era's own docstring).
# Shipping that into fetchers/mlb.py would shift live output and add
# thousands of boxscore fetches to every daily production run for a number
# the live path never asked to have changed.
#
# So it lives here instead, as a monkeypatch of `mlb.team_season_bullpen_era`
# installed only inside THIS process. generate_stats.py and
# generate_insights.py import fetchers.mlb fresh in their own process and
# never load backtest_season.py, so they never see this patch -- the diff
# this file makes to fetchers/mlb.py is exactly zero; see the commit.
#
# _build_one_game calls `team_season_bullpen_era(session, base_url, team_id,
# season, cache)` by bare name, resolved from fetchers.mlb's own module
# globals at call time -- reassigning `mlb.team_season_bullpen_era` redirects
# that call without touching a line of fetchers/mlb.py. The replacement needs
# an `as_of_date` the original signature has no room for; smuggled in through
# the small piece of module state below rather than widening the signature,
# which would break the drop-in match _build_one_game's call site expects.

_pit_state = {"as_of_date": None, "boxscore_cache": None, "era_cache": {}}


def _point_in_time_team_season_bullpen_era(session, base_url, team_id, season, cache):
    """Drop-in replacement for mlb.team_season_bullpen_era, active only while
    this script's monkeypatch is installed (see bottom of this section).
    Reconstructs the team's GS=0 (relief) ERA from real boxscores for every
    Final game from `{season}-01-01` through the day BEFORE
    `_pit_state["as_of_date"]` -- team_bullpen_era's own mechanism, just with
    a season-length window instead of a 7-day one.

    Boxscores are cached in `_pit_state["boxscore_cache"]` -- the SAME dict
    object threaded through this script's whole date loop (see main()), never
    pruned the way build_game_entities' own returned cache is. A game fetched
    once, for any team's window on any date, is never fetched again for the
    rest of the run: the total cost this adds over a full season is bounded
    by the number of distinct games in it (~1,774 for 2026), not by
    teams x days x a season-length window.
    """
    as_of_date = _pit_state["as_of_date"]
    key = (team_id, as_of_date)
    if key in _pit_state["era_cache"]:
        return _pit_state["era_cache"][key]
    boxscore_cache = _pit_state["boxscore_cache"]
    start = "{}-01-01".format(season)
    end = (datetime.date.fromisoformat(as_of_date) - datetime.timedelta(days=1)).isoformat()
    er = outs = 0
    try:
        sched = mlb._get(session, "{}/schedule".format(base_url),
                         params={"sportId": 1, "teamId": team_id, "startDate": start, "endDate": end})
        for d in sched.get("dates", []):
            for g in d.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                pk = str(g.get("gamePk"))
                entry = boxscore_cache.get(pk)
                if entry is None:
                    entry = mlb._bullpen_lines_from_boxscore(
                        mlb._get(session, "{}/game/{}/boxscore".format(base_url, pk)))
                    boxscore_cache[pk] = entry
                line = entry.get(str(team_id))
                if line:
                    er += line["er"]
                    outs += line["ip_outs"]
    except requests.RequestException:
        pass  # best-effort, matching the original function's own fallback
    out = (None, 0.0) if outs == 0 else (round(9.0 * er / (outs / 3.0), 2), round(outs / 3.0, 1))
    _pit_state["era_cache"][key] = out
    return out


# Installed at import time, not inside main(): every entry point this module
# offers (CLI, a future caller importing backtest_date directly) should get
# the point-in-time version, and there is no live entry point that imports
# this module at all.
mlb.team_season_bullpen_era = _point_in_time_team_season_bullpen_era


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
    (games happened, nothing cleared the score floor).

    `boxscore_cache` is returned UNPRUNED -- deliberately not the pruned dict
    build_game_entities itself returns (which keeps only the 7-day window's
    gamePks, by design, to keep the live committed cache small). This script
    never commits the cache anywhere, so there is no size to protect, and the
    point-in-time bullpen reconstruction above needs exactly the games
    pruning would discard: a game far outside any team's 7-day window today
    is often still inside some team's season-to-date window.
    """
    _pit_state["as_of_date"] = date
    _pit_state["boxscore_cache"] = boxscore_cache
    entities, _pruned_cache, _training_rows = mlb.build_game_entities(
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
