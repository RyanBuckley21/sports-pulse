"""Do the shipped NFL picks beat the closing line?

    python3 nfl_odds_backtest.py                       # 2015-2025
    python3 nfl_odds_backtest.py --seasons 2018 2019   # a smaller window

WHY THIS EXISTS. Every performance number in this project is a STRAIGHT-UP hit
rate carrying the same caveat: "on games where the favourite is obvious and the
market knows it, NOT a claim of edge". That caveat was unavoidable because no
odds data enters the pipeline anywhere. nflverse's games.csv carries closing
American moneylines with complete coverage back past 2010, so for this ONE
sport the caveat can be replaced with a measurement -- and it has been. See the
result recorded in config.yaml's betting_signals.nfl block.

WHAT IT MEASURES. The SHIPPED model: nfl_signals.score_game against real
point-in-time team form, with config.yaml's own weights and standout_threshold,
one standout pick per game, settled at the closing price for the side actually
picked. Flat one-unit stakes, because a staking scheme would measure the
staking scheme.

IN-SAMPLE VERSUS OUT, and this is the whole reason the split is reported
separately. The shipped weights were calibrated on 2021-2025
(nfl_backtest.DEFAULT_SEASONS), so those seasons cannot answer the question
about themselves. 2015-2020 is out-of-sample and is the number that counts.

POINT-IN-TIME THROUGHOUT. Every input is built through fetchers/nfl's own
helpers, which filter strictly before the game's week -- team form, scoring
margins, the starting-QB lookup and the injury check alike. The closing line is
the only thing read from the game's own row, and it is used to PRICE the pick,
never to make it.

WHAT IT CANNOT TELL YOU. Closing lines are the sharpest price of the week, so
beating them is a high bar and losing to them narrowly is the expected result
for any model with no price input. This measures the model against that bar; it
says nothing about the softer numbers available earlier in the week, which this
project holds no data for.

Network: two nflverse fetches per season (team stats, injuries) plus one shared
schedule CSV. No API key of any kind.
"""

import argparse
import random
import statistics
import sys

import requests
import yaml

from fetchers import nfl

CONFIG_PATH = "config.yaml"
# nfl_backtest.DEFAULT_SEASONS -- the window the shipped weights were fit on.
CALIBRATION_SEASONS = set(range(2021, 2026))
DEFAULT_SEASONS = list(range(2015, 2026))
BOOTSTRAP_ITERS = 4000
BOOTSTRAP_SEED = 17


def american_to_decimal(value):
    """Closing American odds -> decimal. None for a blank or unparseable cell.

    games.csv stores these as bare signed integers ('124', '-148'), and a
    missing one is 'NA' rather than empty, which float() rejects anyway."""
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return 1.0 + (n / 100.0 if n > 0 else 100.0 / abs(n))


def collect_picks(config, seasons, session=None):
    """Every standout pick the shipped model would have made, priced."""
    session = session or requests.Session()
    schedule_all = nfl.get_schedule(session)
    bar = ((config.get("betting_signals") or {}).get("nfl") or {}).get(
        "standout_threshold", 50)
    picks = []
    for season in seasons:
        sched = [r for r in schedule_all if r.get("season") == str(season)]
        if not sched:
            continue
        team_stats = nfl.get_team_stats(session, season)
        injuries = nfl.get_injuries(session, season)
        prior = nfl.build_prior_season_margin(
            [r for r in schedule_all if r.get("season") == str(season - 1)])
        for game in sched:
            if game.get("game_type") != "REG":
                continue
            home, away = nfl._num(game.get("home_score")), nfl._num(game.get("away_score"))
            if home is None or away is None:
                continue
            try:
                entity = nfl._build_one_game(config, game, sched, team_stats,
                                             injuries, prior)
            except Exception as exc:  # noqa: BLE001 -- one bad game must not cost the season
                print("nfl-odds: {} failed to build ({}: {}); skipped".format(
                    game.get("game_id"), type(exc).__name__, str(exc)[:120]),
                    file=sys.stderr)
                continue
            standout = entity.get("standout")
            if not standout or standout.get("score", 0) < bar:
                continue
            side = str(standout["side"]).split(" ")[0]
            if side not in (game["home_team"], game["away_team"]):
                continue
            picked_home = side == game["home_team"]
            decimal = american_to_decimal(
                game.get("home_moneyline") if picked_home else game.get("away_moneyline"))
            if decimal is None:
                continue
            if home == away:
                pnl, result = 0.0, "PUSH"        # a tie returns the stake
            elif picked_home == (home > away):
                pnl, result = decimal - 1.0, "HIT"
            else:
                pnl, result = -1.0, "MISS"
            picks.append({"season": season, "week": int(game["week"]),
                          "score": standout["score"], "decimal": decimal,
                          "pnl": pnl, "result": result, "favourite": decimal < 2.0})
    return picks


def bootstrap_mean_ci(values, iters=BOOTSTRAP_ITERS, seed=BOOTSTRAP_SEED):
    """95% CI on the mean, stdlib only and fixed seed -- the same procedure
    every other calibration in this repo uses."""
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def summarize(label, picks, minimum=30):
    if len(picks) < minimum:
        print("  {:<22} n={:<5} (too few to report)".format(label, len(picks)))
        return
    pnl = [p["pnl"] for p in picks]
    decided = sum(1 for p in picks if p["result"] != "PUSH")
    hits = sum(1 for p in picks if p["result"] == "HIT")
    roi = sum(pnl) / len(pnl)
    lo, hi = bootstrap_mean_ci(pnl)
    avg_decimal = statistics.fmean(p["decimal"] for p in picks)
    # The win rate the average closing price demands just to break even.
    break_even = 100.0 / avg_decimal
    verdict = ("BEATS THE LINE" if lo > 0 else
               "LOSES TO IT" if hi < 0 else "indistinguishable")
    print("  {:<22} n={:<5} hit {:5.1f}%  avg price {:.2f}  need {:4.1f}%  "
          "ROI {:+6.2f}%  CI [{:+.1f}%, {:+.1f}%]  {}".format(
              label, len(picks), 100.0 * hits / decided, avg_decimal, break_even,
              100 * roi, 100 * lo, 100 * hi, verdict))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", nargs="+", type=int, default=DEFAULT_SEASONS)
    parser.add_argument("--config", default=CONFIG_PATH)
    args = parser.parse_args(argv)

    with open(args.config) as fh:
        config = yaml.safe_load(fh)
    picks = collect_picks(config, args.seasons)
    if not picks:
        print("nfl-odds: no priced picks in {}".format(args.seasons))
        return 1

    out = [p for p in picks if p["season"] not in CALIBRATION_SEASONS]
    ins = [p for p in picks if p["season"] in CALIBRATION_SEASONS]

    print("\n{} picks over {}-{}, flat 1-unit stakes at the closing moneyline"
          .format(len(picks), min(args.seasons), max(args.seasons)))
    print("{}-resample bootstrap CI, seed {}\n".format(BOOTSTRAP_ITERS, BOOTSTRAP_SEED))
    summarize("OUT-OF-SAMPLE", out)
    summarize("in-sample (calibrated)", ins)
    summarize("all seasons", picks)

    print("\nout-of-sample, by Signal Score band")
    for lo, hi in ((50, 64), (65, 79), (80, 100)):
        summarize("  score {}-{}".format(lo, hi),
                  [p for p in out if lo <= p["score"] <= hi])
    print("\nout-of-sample, by price")
    summarize("  favourite (<2.00)", [p for p in out if p["favourite"]])
    summarize("  underdog (>=2.00)", [p for p in out if not p["favourite"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
