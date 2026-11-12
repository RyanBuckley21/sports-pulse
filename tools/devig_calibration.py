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

WHICH METHOD (--methods, added 2026-11-12). Proportional is the simplest of
several ways to take the vig out, and the published guidance is that it is the
weakest where a market has a favourite-longshot bias: it leaves longshots
rated too high and favourites too low, and the power and Shin methods correct
for it (the R `implied` package's documentation calls basic normalisation the
least accurate of its methods; Shin 1993). That is a claim about other
markets, so it was measured here, on decided regular-season games 2010-2025
(4,161), each method's log loss against the result and its paired difference
from proportional (2,000 resamples, seed 17):

    proportional  0.60936
    power         0.60911   -0.00025 [-0.00092, +0.00044]
    odds-ratio    0.60913   -0.00023 [-0.00067, +0.00024]
    additive      0.60915   -0.00021 [-0.00064, +0.00022]
    Shin          0.60915   -0.00021 [-0.00063, +0.00022]

Every alternative is better by a hair and none by an amount this sample can
tell from zero. The bias is visible in the direction the literature says --
under proportional, sides at -500 or shorter won 2.8 points MORE than rated
and sides at +200 or longer 1.4 points LESS, and power flattens both ends to
about a point -- but across the prices a boost leg can be (-250 to +100) the
methods agree to within half a point. So parlay_boost keeps proportional:
there is no measured gain to switch for, and the legs it prices sit where the
choice does not matter. Re-run this if the board ever prices long underdogs.

Network: one nflverse schedule CSV, no key. Writes nothing.
"""

import argparse
import math
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


def _solve(fn, lo, hi):
    """Bisection for the constant that makes a method's two chances sum to 1
    (fn is decreasing in it)."""
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if fn(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def devig_methods(home_dec, away_dec):
    """{method: the home side's chance} for the five two-outcome methods."""
    x, y = 1.0 / home_dec, 1.0 / away_dec
    total = x + y
    k = _solve(lambda k: x ** k + y ** k, 0.5, 3.0)
    odds = lambda p, c: p / (c + p - c * p)                      # noqa: E731
    c = _solve(lambda c: odds(x, c) + odds(y, c), 1.0, 3.0)
    z = ((total - 1.0) * ((x - y) ** 2 - total)) / (total * ((x - y) ** 2 - 1.0))
    return {
        "proportional": x / total,
        "power": x ** k,
        "odds-ratio": odds(x, c),
        "additive": x - (total - 1.0) / 2.0,
        "Shin": (math.sqrt(z * z + 4.0 * (1.0 - z) * x * x / total) - z) / (2.0 * (1.0 - z)),
    }


def compare_methods(seasons, session=None, iters=2000, seed=BOOTSTRAP_SEED):
    """Print each method's log loss on decided games and its paired difference
    from proportional. Ties are left out: log loss needs a winner."""
    losses = {}
    for g in nfl.get_schedule(session or requests.Session()):
        if g.get("game_type") != "REG" or g.get("season") not in {str(s) for s in seasons}:
            continue
        hs, as_ = nfl._num(g.get("home_score")), nfl._num(g.get("away_score"))
        hd = espn_odds.american_to_decimal(g.get("home_moneyline"))
        ad = espn_odds.american_to_decimal(g.get("away_moneyline"))
        if None in (hs, as_, hd, ad) or hs == as_:
            continue
        won = hs > as_
        for name, p in devig_methods(hd, ad).items():
            p = min(max(p if won else 1.0 - p, 1e-9), 1.0)
            losses.setdefault(name, []).append(-math.log(p))
    n = len(losses.get("proportional", []))
    if not n:
        print("devig: no decided priced games in {}".format(seasons))
        return 1
    print("{} decided games, {}-{}: log loss by method, and minus proportional".format(
        n, min(seasons), max(seasons)))
    base = losses["proportional"]
    rng = random.Random(seed)
    for name, vals in losses.items():
        line = "  {:<13} {:.5f}".format(name, sum(vals) / n)
        if name != "proportional":
            diff = [a - b for a, b in zip(vals, base)]
            means = sorted(sum(diff[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
            line += "   {:+.5f} [{:+.5f}, {:+.5f}]".format(
                sum(diff) / n, means[int(0.025 * iters)], means[int(0.975 * iters)])
        print(line)
    return 0


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
    parser.add_argument("--methods", action="store_true",
                        help="compare five de-vig methods by log loss instead")
    args = parser.parse_args(argv)
    if args.methods:
        return compare_methods(args.seasons)
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
