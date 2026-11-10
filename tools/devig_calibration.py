"""How well does a proportional no-vig probability predict NFL results?

    python3 -m tools.devig_calibration                 # 2015-2025
    python3 -m tools.devig_calibration --seasons 2022 2023

WHY THIS EXISTS. parlay_boost.py prices a boosted parlay from each leg's
"fair" win chance, and takes that chance from the market itself: both sides'
moneylines with the vig removed proportionally, p = (1/d_side) / (1/d_home +
1/d_away). That method has no fitted constant, but it is still a claim -- that
the book's margin is spread across the two sides in proportion -- and a wrong
claim would bias every EV the Bets tray prints. This measures it: for every
side of every regular-season game with both closing moneylines, the realised
win rate minus the no-vig probability, by price band, with the repo's usual
bootstrap (4,000 resamples, seed 17).

A positive number means sides in that band won MORE often than the no-vig
price said (the proportional method under-rates them); negative, less.

WHAT IT IS NOT. Not a model result -- no pick is involved, every side is
counted. And closing lines only: nflverse carries no earlier price.

Network: one nflverse schedule CSV, no key. Writes nothing.
"""

import argparse
import random
import sys

import requests

import espn_odds
from fetchers import nfl

BOOTSTRAP_ITERS = 4000
BOOTSTRAP_SEED = 17
# American price bands for the side being rated, favourite end first. The
# -300..-150 band is where DraftKings' usual per-leg floors (-200, -250, -300)
# put a parlay leg; the rest is context.
BANDS = (("-500 or shorter", None, -500), ("-499..-301", -499, -301),
         ("-300..-200", -300, -200), ("-199..-150", -199, -150),
         ("-149..-101", -149, -101), ("+100..+199", 100, 199),
         ("+200 or longer", 200, None))


def no_vig(home_dec, away_dec):
    inv_h, inv_a = 1.0 / home_dec, 1.0 / away_dec
    return inv_h / (inv_h + inv_a), inv_a / (inv_h + inv_a)


def bootstrap_mean_ci(values, iters=BOOTSTRAP_ITERS, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def collect(seasons, session=None):
    """[(american, residual)] -- one entry per side: its closing American price
    and (won - no-vig probability), a tie counting half."""
    out = []
    for g in nfl.get_schedule(session or requests.Session()):
        if g.get("game_type") != "REG" or g.get("season") not in {str(s) for s in seasons}:
            continue
        hs, as_ = nfl._num(g.get("home_score")), nfl._num(g.get("away_score"))
        hd = espn_odds.american_to_decimal(g.get("home_moneyline"))
        ad = espn_odds.american_to_decimal(g.get("away_moneyline"))
        if None in (hs, as_, hd, ad):
            continue
        ph, pa = no_vig(hd, ad)
        home_won = 0.5 if hs == as_ else float(hs > as_)
        out.append((float(g["home_moneyline"]), home_won - ph))
        out.append((float(g["away_moneyline"]), (1.0 - home_won) - pa))
    return out


def in_band(american, lo, hi):
    return (lo is None or american >= lo) and (hi is None or american <= hi)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", nargs="+", type=int, default=list(range(2015, 2026)))
    args = parser.parse_args(argv)
    sides = collect(args.seasons)
    if not sides:
        print("devig: no priced games in {}".format(args.seasons))
        return 1
    print("{} sides, {}-{}, closing moneylines; realised win rate minus the "
          "proportional no-vig probability".format(len(sides), min(args.seasons), max(args.seasons)))
    for label, lo, hi in BANDS:
        vals = [res for american, res in sides if in_band(american, lo, hi)]
        if len(vals) < 30:
            print("  {:<16} n={:<5} (too few)".format(label, len(vals)))
            continue
        low, high = bootstrap_mean_ci(vals)
        print("  {:<16} n={:<5} {:+5.2f}pp  [{:+.2f}, {:+.2f}]".format(
            label, len(vals), 100 * sum(vals) / len(vals), 100 * low, 100 * high))
    return 0


if __name__ == "__main__":
    sys.exit(main())
