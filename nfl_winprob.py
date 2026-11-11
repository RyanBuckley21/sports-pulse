"""NFL win probability: a price-blind, calibrated chance for every game, refit
from results, and graded against the closing price.

    python3 nfl_winprob.py --audit     # the measurements below, re-derived
    python3 nfl_winprob.py --fit       # refit on every completed game, rewrite
                                       # data/nfl_winprob.json (the weekly job)

WHY THIS EXISTS. The owner's brief, 2026-11-10: "I want predictors in NFL ...
I want the models to be graded, learn from those grades, and adjust." The
Signal Score cannot do that. It is a 0-100 conviction with hand-shaped tanh
scales and fixed weights; it names a side only above a threshold, it is not a
probability, and nothing about it changes when a result comes in. This is the
same information (the same seven inputs, built by the same fetcher) as a
logistic regression whose weights ARE the fit to every completed game, so
"learning" is literal: re-run the fit and the numbers move.

WHAT IT IS NOT, MEASURED BEFORE IT WAS BUILT (--audit, 2026-11-10: 4,297
decided games 2010 to week 10 of 2026, 4,296 with both closing moneylines;
walk-forward 2015-2026 = 3,020 games, each season predicted from all earlier
ones; 4,000-resample intervals, 1,000 for the correlations):

  Each input predicts results and NONE predicts what the price left over
  (r with result | r with result-minus-no-vig-price):
      scoring margin        +0.25 | -0.01 [-0.04, +0.02]
      offensive EPA         +0.24 | +0.02 [-0.01, +0.05]
      prior-season margin   +0.22 | +0.01 [-0.02, +0.04]
      turnover differential +0.13 | +0.01 [-0.02, +0.04]
      defensive EPA allowed +0.07 | -0.04 [-0.07, -0.01]
      QB out                +0.07 | +0.01 [-0.02, +0.04]
      rest difference       +0.03 | +0.01 [-0.02, +0.04]

  The fitted model beats always-home and loses to the price, on the same games:
      always-home rate    accuracy 54.4%   log loss 0.690
      this model          accuracy 62.9%   log loss 0.647
      closing price       accuracy 66.4%   log loss 0.613
      model minus price, log loss  +0.034 [+0.026, +0.043]

  Adding it to the price improves nothing (a one-off stacked fit, walk-forward
  2018-2025, not part of --audit): -0.0004 [-0.0012, +0.0004]

  WHERE IT DISAGREES WITH THE PRICE, THE PRICE IS RIGHT. When the model rates
  a side at least 5 points above the no-vig price, that side wins 2.4 points
  LESS often than the price says [-4.5, -0.3] (n=1,829); at 10 points -2.1
  [-5.0, +0.9]; at 15, -3.5 [-7.5, +0.7]. And singles at -150 or longer on
  sides it rates 10+ points above break-even return -10.4% [-20.3, +0.0],
  against -4.3% for every side in that range. Three thresholds were looked at,
  fixed before looking (0.05/0.10/0.15); only the first excludes zero, and
  only just. That is the basis for bet_board's caution note and it is stated
  there as thin.

  So this is a PREDICTOR, NOT AN EDGE. It is the best price-blind estimate
  this repo has of who wins, and it is worse than the market's. Its uses are
  the ones that do not need it to beat the market: a pick and a stated chance
  for every game, something that can be graded honestly week by week, and a
  flag for leans the market does not share. Re-run --audit before claiming
  anything more.

PRICE-BLIND (invariant 1). No odds, line or market number is an input, in
fitting or in prediction. Prices appear only in --audit, to grade it.

THE MODEL. P(home wins) = logistic(w . x), x being, in order:
      home          1 for a true home game, 0 at a neutral site
      off, deff, to, margin, prior, rest
                    home-minus-away gaps of the builder's own inputs, each
                    standardised by the training mean and sd; a missing gap is
                    0 (the average game) rather than a dropped row
      qb            +1 away QB officially out, -1 home QB out
      no_epa, no_margin
                    1 when that gap is missing (week 1 has no EPA; margin
                    needs a game played), so "unknown" is not read as "even"
  fitted by Newton's method with an L2 penalty of 1.0. THE PENALTY DOES NOT
  MATTER and is not tuned: walk-forward log loss across 0.01 / 0.1 / 1 / 10 /
  100 is 0.6466 / 0.6466 / 0.6466 / 0.6463 / 0.6456 (1000: 0.6569), measured
  once on 2015-2025 with a throwaway numpy copy of this fit. The whole
  spread is 0.001 against a 0.034 gap to the price.
  Ties are left out of the fit (one result in ~250, and not a home win or loss).

LEARNING IS A REFIT, NOT A TWEAK. --fit rebuilds every completed regular-
season game from 2010 through the latest, fits once on all of them, and
writes data/nfl_winprob.json with the walk-forward scorecard beside the
weights (invariant 8: the derivation travels with the number). It REFUSES to
write when the new fit is not better than always-home walk-forward, or when
it found fewer games than the file it would replace -- a broken feed must
not be able to teach the model.

Stdlib only. Network: --audit and --fit read nflverse through fetchers/nfl
(two fetches a season plus the schedule); predict() reads nothing.
"""

import argparse
import datetime
import json
import math
import os
import random
import sys

MODEL_PATH = "data/nfl_winprob.json"
SCHEMA_VERSION = 1
FIRST_SEASON = 2010          # 16 seasons to 2025; earlier ones were not tested
FIRST_TEST_SEASON = 2015     # so the first fold trains on five seasons
L2 = 1.0                     # see THE PENALTY DOES NOT MATTER
NEWTON_STEPS = 12            # converges in 5-6; the rest is margin
BOOTSTRAP_ITERS = 4000
BOOTSTRAP_SEED = 17
# The disagreement at which bet_board notes a lean the market does not share.
# One of three thresholds fixed before measuring (0.05 / 0.10 / 0.15) and the
# only one whose interval excludes zero: -2.4pp [-4.5, -0.3], n=1,829.
DISAGREE_MIN = 0.05

GAPS = ("off", "deff", "to", "margin", "prior", "rest")
COLUMNS = ("home",) + GAPS + ("qb", "no_epa", "no_margin")
# builder input key (without its home_/away_ prefix) for each gap
_INPUT_KEYS = {"off": "off_epa", "deff": "def_epa_allowed", "to": "turnover_diff",
               "margin": "scoring_margin", "prior": "prior_margin", "rest": "rest"}


# ------------------------------------------------------------------ features
def features(inputs, availability=None):
    """The raw, unstandardised facts the model reads for one game, from the
    dict nfl_signals.build_inputs returns. Every gap is HOME MINUS AWAY in the
    input's own units, or None when either side is missing. Stored on the
    game entity as-is, so a later refit -- or a different model -- can be
    applied to games already played without rebuilding them."""
    availability = availability or {}
    out = {}
    for name, key in _INPUT_KEYS.items():
        home, away = inputs.get("home_" + key), inputs.get("away_" + key)
        out[name] = None if home is None or away is None else float(home) - float(away)
    out["qb"] = (1 if availability.get("away_qb_out") else 0) - \
                (1 if availability.get("home_qb_out") else 0)
    out["neutral"] = bool(inputs.get("neutral_site"))
    return out


def _vector(feats, mu, sd):
    row = [0.0 if feats.get("neutral") else 1.0]
    for name in GAPS:
        value = feats.get(name)
        scale = sd.get(name) or 0.0
        row.append(0.0 if value is None or scale <= 0 else (value - mu[name]) / scale)
    row.append(float(feats.get("qb") or 0))
    row.append(1.0 if feats.get("off") is None else 0.0)
    row.append(1.0 if feats.get("margin") is None else 0.0)
    return row


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def predict(model, feats):
    """P(home wins) for one game's features under a fitted model, or None
    when there is no usable model. Never raises: a missing or damaged model
    file must cost the board a probability, not the slate."""
    try:
        weights = model["weights"]
        row = _vector(feats, model["mu"], model["sd"])
        return _sigmoid(sum(weights[c] * x for c, x in zip(COLUMNS, row)))
    except Exception:  # noqa: BLE001 -- see docstring
        return None


def load_model(path=MODEL_PATH):
    """The committed fit, or None if it is absent or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            model = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(model, dict) or set(COLUMNS) - set(model.get("weights") or {}):
        return None
    return model


_CACHE = {}


def current_model(path=MODEL_PATH):
    """The committed fit, read once per process (a slate builds ~16 games)."""
    if path not in _CACHE:
        _CACHE[path] = load_model(path)
    return _CACHE[path]


def game_block(inputs, availability=None, model=None):
    """What a game entity carries as `win_prob`: the raw features ALWAYS (they
    are what a later refit learns from), and the model's chance for each side
    when a fitted model is on file. `home`/`away` are None without one -- a
    missing model costs the probability, never the game."""
    feats = features(inputs, availability)
    model = current_model() if model is None else model
    p = predict(model, feats) if model else None
    return {"home": None if p is None else round(p, 4),
            "away": None if p is None else round(1.0 - p, 4),
            "fitted_at": (model or {}).get("fitted_at"),
            "features": feats}


def display(block, home_abbr, away_abbr, odds=None):
    """What a card prints for a game: the side the model makes more likely
    than not, its chance, and -- when both sides are priced -- the MARKET's
    chance for that same side, so the two are always read together. None
    without a fitted chance. The market number is joined here, at display;
    it is never an input (invariant 1)."""
    if not block or block.get("home") is None:
        return None
    home_picked = block["home"] >= 0.5
    out = {"side": home_abbr if home_picked else away_abbr,
           "chance": block["home"] if home_picked else block["away"],
           "market_chance": None, "fitted_at": block.get("fitted_at")}
    try:
        import parlay_boost
        fair = parlay_boost.fair_prob(odds, home_picked)
    except Exception:  # noqa: BLE001 -- display only
        fair = None
    if fair is not None:
        out["market_chance"] = round(fair, 4)
    return out


def unchanged(new_model, old_model):
    """True when a refit saw no game the model on file had not: same games,
    same span. The weekly job then leaves the file alone, so a quiet week (or
    a second cron entry firing after the first) makes no commit."""
    return bool(old_model) and old_model.get("games") == new_model["games"] \
        and old_model.get("seasons") == new_model["seasons"] \
        and old_model.get("schema_version") == new_model["schema_version"]


# ----------------------------------------------------------------------- fit
def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting; len(COLUMNS) is 10."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        out[r] = (a[r][n] - sum(a[r][c] * out[c] for c in range(r + 1, n))) / a[r][r]
    return out


def fit(rows, l2=L2):
    """Fit on `rows` ({"features": ..., "home_won": 0/1}); returns the model
    dict's numeric core: mu, sd, weights."""
    mu, sd = {}, {}
    for name in GAPS:
        values = [r["features"][name] for r in rows if r["features"].get(name) is not None]
        mean = sum(values) / len(values)
        mu[name] = mean
        sd[name] = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    xs = [_vector(r["features"], mu, sd) for r in rows]
    ys = [float(r["home_won"]) for r in rows]
    k = len(COLUMNS)
    w = [0.0] * k
    for _ in range(NEWTON_STEPS):
        grad = [l2 * w[j] for j in range(k)]
        hess = [[(l2 if i == j else 0.0) for j in range(k)] for i in range(k)]
        for x, y in zip(xs, ys):
            p = _sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            err, curve = p - y, p * (1.0 - p)
            for i in range(k):
                xi = x[i]
                if xi:
                    grad[i] += err * xi
                    cx = curve * xi
                    hi = hess[i]
                    for j in range(k):
                        hi[j] += cx * x[j]
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-10:
            break
    return {"mu": mu, "sd": sd, "weights": dict(zip(COLUMNS, w))}


def walk_forward(rows, first_test=FIRST_TEST_SEASON):
    """Each season from `first_test` predicted by a fit on all EARLIER seasons.
    Returns the test rows, each with `p_model` and `p_home_rate` (the home win
    rate of its own training seasons -- the always-home baseline, measured on
    the same split rather than quoted from elsewhere)."""
    out = []
    for season in sorted({r["season"] for r in rows if r["season"] >= first_test}):
        train = [r for r in rows if r["season"] < season]
        if not train:
            continue
        model = fit(train)
        rate = sum(r["home_won"] for r in train) / len(train)
        for r in rows:
            if r["season"] == season:
                out.append(dict(r, p_model=predict(model, r["features"]), p_home_rate=rate))
    return out


def log_loss(p, won):
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    return -math.log(p if won else 1.0 - p)


def mean_ci(values, iters=BOOTSTRAP_ITERS, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    n = len(values)
    index = range(n)
    means = sorted(sum(values[i] for i in rng.choices(index, k=n)) / n for _ in range(iters))
    return sum(values) / n, means[int(0.025 * iters)], means[int(0.975 * iters)]


def scorecard(tested, with_current=True):
    """The walk-forward summary stored beside the weights. `current_season` is
    the same card for the latest season alone: THE GRADE SO FAR THIS YEAR, by
    fits that had never seen it. It is the number to watch week to week, and
    it is small (a season is 272 games), so it carries its own interval."""
    n = len(tested)
    ll_model = [log_loss(r["p_model"], r["home_won"]) for r in tested]
    ll_home = [log_loss(r["p_home_rate"], r["home_won"]) for r in tested]
    diff = mean_ci([a - b for a, b in zip(ll_model, ll_home)])
    card = {
        "seasons": [min(r["season"] for r in tested), max(r["season"] for r in tested)],
        "games": n,
        "accuracy": sum((r["p_model"] > 0.5) == bool(r["home_won"]) for r in tested) / n,
        "log_loss": sum(ll_model) / n,
        "always_home_accuracy": sum(r["home_won"] for r in tested) / n,
        "always_home_log_loss": sum(ll_home) / n,
        "minus_always_home_log_loss": {"mean": diff[0], "ci95": [diff[1], diff[2]]},
    }
    priced = [r for r in tested if r.get("p_market") is not None]
    if priced:
        gap = mean_ci([log_loss(r["p_model"], r["home_won"]) - log_loss(r["p_market"], r["home_won"])
                       for r in priced])
        card["price"] = {
            "games": len(priced),
            "accuracy": sum((r["p_market"] > 0.5) == bool(r["home_won"]) for r in priced) / len(priced),
            "log_loss": sum(log_loss(r["p_market"], r["home_won"]) for r in priced) / len(priced),
            "model_minus_price_log_loss": {"mean": gap[0], "ci95": [gap[1], gap[2]]},
        }
    if with_current:
        latest = card["seasons"][1]
        this_year = [r for r in tested if r["season"] == latest]
        if len(this_year) >= 30:
            card["current_season"] = dict(scorecard(this_year, with_current=False), season=latest)
    return card


# ------------------------------------------------------- collecting the games
def collect(seasons, session=None, config=None):
    """One row per DECIDED regular-season game: the features the live builder
    would have computed before kickoff (fetchers.nfl._build_one_game, which is
    point-in-time throughout), the result, and -- for grading only -- the
    no-vig closing probability where both moneylines exist."""
    import requests
    import yaml
    import parlay_boost
    from fetchers import nfl
    session = session or requests.Session()
    if config is None:
        with open("config.yaml") as fh:
            config = yaml.safe_load(fh)
    schedule_all = nfl.get_schedule(session)
    rows = []
    for season in seasons:
        sched = [r for r in schedule_all if r.get("season") == str(season)]
        if not sched:
            continue
        try:
            team_stats = nfl.get_team_stats(session, season)
            injuries = nfl.get_injuries(session, season)
        except Exception as exc:  # noqa: BLE001 -- a season not yet published
            print("nfl-winprob: season {} skipped ({}: {})".format(
                season, type(exc).__name__, str(exc)[:100]), file=sys.stderr)
            continue
        prior = nfl.build_prior_season_margin(
            [r for r in schedule_all if r.get("season") == str(season - 1)])
        for game in sched:
            if game.get("game_type") != "REG":
                continue
            hs, as_ = nfl._num(game.get("home_score")), nfl._num(game.get("away_score"))
            if hs is None or as_ is None or hs == as_:
                continue
            try:
                entity = nfl._build_one_game(config, game, sched, team_stats, injuries, prior)
            except Exception:  # noqa: BLE001 -- one bad game must not cost the season
                continue
            feats = (entity.get("win_prob") or {}).get("features")
            if not feats:
                continue
            pair = parlay_boost.no_vig(game.get("home_moneyline"), game.get("away_moneyline"))
            rows.append({"season": season, "week": int(game["week"]),
                         "home": game["home_team"], "away": game["away_team"],
                         "features": feats, "home_won": 1 if hs > as_ else 0,
                         "p_market": pair[0] if pair else None,
                         "home_ml": game.get("home_moneyline"),
                         "away_ml": game.get("away_moneyline")})
    return rows


def build_model(rows, today=None):
    """The full model file: one fit on every row, plus the walk-forward
    scorecard that says how fits like it have done on seasons they had not
    seen."""
    core = fit(rows)
    seasons = sorted({r["season"] for r in rows})
    return dict(core, schema_version=SCHEMA_VERSION,
                fitted_at=(today or datetime.date.today()).isoformat(),
                seasons=[seasons[0], seasons[-1]], games=len(rows), l2=L2,
                columns=list(COLUMNS),
                walk_forward=scorecard(walk_forward(rows)),
                derivation="python3 nfl_winprob.py --fit; see the module docstring")


def refusal(new_model, old_model):
    """Why a refit must NOT replace the committed model, or None."""
    card = new_model["walk_forward"]
    if card["minus_always_home_log_loss"]["ci95"][1] >= 0:
        return ("walk-forward log loss is not better than always-home "
                "(interval {:+.4f}..{:+.4f})".format(*card["minus_always_home_log_loss"]["ci95"]))
    if old_model and new_model["games"] < old_model.get("games", 0):
        return "found {} games, fewer than the {} behind the current model".format(
            new_model["games"], old_model.get("games"))
    return None


# --------------------------------------------------------------------- report
def _corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def _corr_ci(xs, ys, iters=1000, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    n = len(xs)
    index = range(n)
    draws = []
    for _ in range(iters):
        pick = rng.choices(index, k=n)
        draws.append(_corr([xs[i] for i in pick], [ys[i] for i in pick]))
    draws.sort()
    return _corr(xs, ys), draws[int(0.025 * iters)], draws[int(0.975 * iters)]


def audit(rows):
    signs = {"off": 1, "deff": -1, "to": 1, "margin": 1, "prior": 1, "rest": 1, "qb": 1}
    priced = [r for r in rows if r["p_market"] is not None]
    print("{} decided games, {} with both closing moneylines".format(len(rows), len(priced)))
    print("\nA. each input: r with the result | r with (result - no-vig closing price)")
    for name in GAPS + ("qb",):
        sub = [r for r in priced if r["features"].get(name) is not None]
        xs = [signs[name] * r["features"][name] for r in sub]
        res = _corr_ci(xs, [r["home_won"] - r["p_market"] for r in sub])
        print("  {:<7} n={:<5} {:+.3f} | {:+.3f} [{:+.3f}, {:+.3f}]".format(
            name, len(sub), _corr(xs, [r["home_won"] for r in sub]), *res))

    tested = walk_forward(rows)
    card = scorecard(tested)
    print("\nB. walk-forward {}-{}, {} games".format(card["seasons"][0], card["seasons"][1], card["games"]))
    print("  always-home   accuracy {:.4f}  log loss {:.4f}".format(
        card["always_home_accuracy"], card["always_home_log_loss"]))
    print("  model         accuracy {:.4f}  log loss {:.4f}".format(card["accuracy"], card["log_loss"]))
    print("  model minus always-home, log loss {:+.4f} [{:+.4f}, {:+.4f}]".format(
        card["minus_always_home_log_loss"]["mean"], *card["minus_always_home_log_loss"]["ci95"]))
    if "price" in card:
        print("  closing price accuracy {:.4f}  log loss {:.4f}  (n={})".format(
            card["price"]["accuracy"], card["price"]["log_loss"], card["price"]["games"]))
        print("  model minus price, log loss {:+.4f} [{:+.4f}, {:+.4f}]".format(
            card["price"]["model_minus_price_log_loss"]["mean"],
            *card["price"]["model_minus_price_log_loss"]["ci95"]))

    tp = [r for r in tested if r["p_market"] is not None]
    print("\nC. where the model disagrees with the price: the model's side, win rate minus no-vig price")
    for threshold in (0.05, 0.10, 0.15):
        vals = []
        for r in tp:
            d = r["p_model"] - r["p_market"]
            if abs(d) >= threshold:
                vals.append((1 if d > 0 else -1) * (r["home_won"] - r["p_market"]))
        if len(vals) >= 30:
            m, lo, hi = mean_ci(vals)
            print("  by >= {:.2f}: n={:<5} {:+.2f}pp [{:+.1f}, {:+.1f}]".format(
                threshold, len(vals), 100 * m, 100 * lo, 100 * hi))

    print("\nD. singles at -150 or longer, the side the model rates above its break-even by the margin")
    import parlay_boost
    floor = parlay_boost.american_to_decimal(-150)

    def sides(r):
        for home in (True, False):
            dec = parlay_boost.american_to_decimal(r["home_ml"] if home else r["away_ml"])
            if dec is not None and dec >= floor - 1e-9:
                yield dec, (r["p_model"] if home else 1.0 - r["p_model"]), bool(r["home_won"]) == home
    for margin in (0.0, 0.03, 0.06, 0.10):
        pnl = [dec - 1.0 if won else -1.0 for r in tp for dec, p, won in sides(r)
               if p - 1.0 / dec >= margin]
        if len(pnl) >= 30:
            m, lo, hi = mean_ci(pnl)
            print("  margin >= {:.2f}: n={:<5} ROI {:+.1f}% [{:+.1f}, {:+.1f}]".format(
                margin, len(pnl), 100 * m, 100 * lo, 100 * hi))
    pnl = [dec - 1.0 if won else -1.0 for r in tp for dec, p, won in sides(r)]
    m, lo, hi = mean_ci(pnl)
    print("  baseline, EVERY side at -150 or longer on the same games: n={} ROI {:+.1f}% [{:+.1f}, {:+.1f}]"
          .format(len(pnl), 100 * m, 100 * lo, 100 * hi))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--fit", action="store_true")
    parser.add_argument("--path", default=MODEL_PATH)
    parser.add_argument("--force", action="store_true",
                        help="rewrite the model even when no new game was found")
    parser.add_argument("--through", type=int, default=datetime.date.today().year)
    args = parser.parse_args(argv)
    if not (args.audit or args.fit):
        parser.error("choose --audit and/or --fit")
    rows = collect(range(FIRST_SEASON, args.through + 1))
    if not rows:
        print("nfl-winprob: no games collected; nothing written", file=sys.stderr)
        return 1
    if args.audit:
        audit(rows)
    if args.fit:
        new = build_model(rows)
        old = load_model(args.path)
        if unchanged(new, old) and not args.force:
            print("nfl-winprob: no new games since the fit of {} ({} games); {} left untouched".format(
                old.get("fitted_at"), old.get("games"), args.path))
            return 0
        why = refusal(new, old)
        if why:
            print("nfl-winprob: REFUSED to replace {} -- {}".format(args.path, why), file=sys.stderr)
            return 1
        with open(args.path, "w", encoding="utf-8") as fh:
            json.dump(new, fh, indent=1, sort_keys=True)
            fh.write("\n")
        card = new["walk_forward"]
        print("nfl-winprob: fitted on {} games {}-{}; walk-forward accuracy {:.4f} "
              "(always-home {:.4f}), log loss {:.4f}".format(
                  new["games"], new["seasons"][0], new["seasons"][1],
                  card["accuracy"], card["always_home_accuracy"], card["log_loss"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
