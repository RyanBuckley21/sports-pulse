"""Regression tests for the NFL win-probability model (nfl_winprob.py), its
place on the game entity, and the Bets row note built from it.

Run: python3 -m tools.verify.test_nfl_winprob   (from the repo root)

WHY THIS IS PINNED. This model is refit by a scheduled job and its output is
read as "the chance this team wins". Every failure below is silent -- the fit
still converges, the page still prints a percentage:

  * LEAKAGE. A walk-forward grade is only a grade if a season's own results
    cannot reach its own predictions. Flipping every result in the test
    season must leave that season's predictions untouched.
  * PRICE-BLIND (invariant 1). No price is a feature, and changing every
    market number in the training rows must leave the weights identical.
  * DIRECTION. Home minus away, and the sign of each input: a better home
    team, an away quarterback ruled out, and a true home venue must each
    RAISE the home chance. One flipped sign fits happily and predicts
    backwards for half the league.
  * UNKNOWN IS NOT EVEN. Week 1 has no EPA; a missing gap must be read as
    "missing" (its own column), not as a matchup of equals.
  * THE FIT IS THE OPTIMUM, and deterministic: the penalised gradient is zero
    at the answer, and two fits of the same rows agree to the last digit.
  * A BAD REFIT CANNOT REPLACE A GOOD MODEL: one no better than always-home,
    or built on fewer games than the model on file, is refused.
  * A MISSING MODEL COSTS THE PROBABILITY, NOT THE SLATE: no file, a damaged
    file and a half-written file all give None, never an exception, and the
    raw features are still recorded.
  * THE NOTE IS A CAUTION ON ONE SIDE ONLY: it appears when the model rates
    the picked side at least 5 points ABOVE the market, never when below.

Fixture: tools/verify/nfl_winprob_fixture.json -- REAL, 1,355 decided games
2021-2025 captured from nflverse through the production builder (provenance
in the file). The Bets rows come from the real bets_fixture.json; that
capture predates this model, so `win_prob` is ADDED to one NFL game there
(LAC @ BUF, one field, the values stated at the assertion) -- the branch has
no other way to be reached from a pre-model capture.

Sabotage-checked when written (2026-11-11, PYTHONDONTWRITEBYTECODE=1, sources
byte-identical afterwards), failures measured, of 63:
  W1  walk-forward trains on the season it tests      3 fail
  W2  gaps are away minus home                        1 fail
  W3  QB-out sign flipped                             1 fail
  W4  missing EPA not flagged                         1 fail
  W5  neutral site ignored                            1 fail
  W6  fit stops after one Newton step                 2 fail
  W7  a fewer-games refit accepted                    1 fail
  W8  a no-better-than-home refit accepted            1 fail
  W9  predict raises without a model                  1 fail
  W10 a file missing columns accepted                 1 fail
  W11 the market number trains the fit                4 fail
  W12 no model: features dropped too                  1 fail
  W13 always-home baseline from ALL rows              1 fail
  W14 a missing gap read as the VALUE zero            2 fail
  D1  market chance always the home side's            1 fail
  D2  display always names the home side              1 fail
  D3  a quiet week never 'unchanged'                  1 fail
  D4  any refit counts as unchanged                   1 fail
  B1  row note fires on either side of the market     1 fail
  B2  row note excludes the 5-point line itself       1 fail
  B3  home side given the away chance                 3 fail
  B4  note dropped from the row                       2 fail
Three checks were wrong when first written and were fixed by this pass: W9
CRASHED the suite (TypeError) instead of failing a check; W14's first cut was
arithmetically equivalent to the real code and proved nothing; and B2 PASSED
under sabotage because "exactly 5 points" was built from a rounded number --
the comparison now carries a 1e-9 tolerance and the test uses the unrounded
market chance.
"""

import copy
import json
import os
import random
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import bet_board  # noqa: E402
import nfl_signals  # noqa: E402
import nfl_winprob as wp  # noqa: E402
import yaml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail)[:300] if detail else ""))


def near(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


ROWS = json.load(open(os.path.join(HERE, "nfl_winprob_fixture.json")))["rows"]
CONFIG = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))
ok("fixture: 1,355 decided games, 2021-2025",
   len(ROWS) == 1355 and {r["season"] for r in ROWS} == {2021, 2022, 2023, 2024, 2025}, len(ROWS))

# ------------------------------------------------------------- 1. features
# A real row's own numbers, pushed through the builder's input dict.
INPUTS = nfl_signals.build_inputs(
    away_abbr="AAA", home_abbr="HHH",
    away_off_epa=-0.05, home_off_epa=0.10,
    away_def_epa_allowed=0.04, home_def_epa_allowed=-0.02,
    away_turnover_diff=-1.0, home_turnover_diff=0.5,
    away_scoring_margin=-3.0, home_scoring_margin=6.0,
    away_rest=7, home_rest=10,
    away_prior_margin=-2.0, home_prior_margin=4.5)
f = wp.features(INPUTS, {"away_qb_out": False, "home_qb_out": False})
ok("every gap is HOME MINUS AWAY, in the input's own units",
   all(near(f[k], v, 1e-9) for k, v in
       {"off": 0.15, "deff": -0.06, "to": 1.5, "margin": 9.0, "prior": 6.5, "rest": 3.0}.items()), f)
ok("no QB out: 0; a true home game: not neutral", f["qb"] == 0 and f["neutral"] is False, f)
ok("away QB out is +1 (toward home), home QB out is -1, both out cancel",
   (wp.features(INPUTS, {"away_qb_out": True})["qb"], wp.features(INPUTS, {"home_qb_out": True})["qb"],
    wp.features(INPUTS, {"away_qb_out": True, "home_qb_out": True})["qb"]) == (1, -1, 0))
week1 = dict(INPUTS, home_off_epa=None, away_off_epa=None)
ok("a gap with either side missing is None, not zero and not the other side's value",
   wp.features(week1)["off"] is None and wp.features(dict(INPUTS, away_scoring_margin=None))["margin"] is None)
ok("neutral site is carried", wp.features(dict(INPUTS, neutral_site=True))["neutral"] is True)

# ------------------------------------------------------------------ 2. fit
MODEL = wp.fit(ROWS)
W = MODEL["weights"]
ok("the fit returns a weight for every column, in order", list(W) == list(wp.COLUMNS), list(W))
ok("fitting the same rows twice gives the identical model", wp.fit(ROWS) == MODEL)
ok("a better home team by margin, last season, and offence each RAISE the home chance",
   W["margin"] > 0 and W["prior"] > 0 and W["off"] > 0, W)
ok("a true home venue and an away QB out both raise it", W["home"] > 0 and W["qb"] > 0, W)

# The penalised gradient is zero at the optimum: sum (p - y) x + L2 w = 0.
xs = [wp._vector(r["features"], MODEL["mu"], MODEL["sd"]) for r in ROWS]
grad = [wp.L2 * W[c] for c in wp.COLUMNS]
for x, r in zip(xs, ROWS):
    p = wp._sigmoid(sum(W[c] * xi for c, xi in zip(wp.COLUMNS, x)))
    for i, xi in enumerate(x):
        grad[i] += (p - r["home_won"]) * xi
ok("the fit is the optimum: the penalised gradient is zero at it",
   max(abs(g) for g in grad) < 1e-6, max(abs(g) for g in grad))
train_rate = sum(r["home_won"] for r in ROWS) / len(ROWS)
mean_p = sum(wp.predict(MODEL, r["features"]) for r in ROWS) / len(ROWS)
ok("on its own training games the mean chance matches the home win rate",
   near(mean_p, train_rate, 0.005), (mean_p, train_rate))
ok("_solve solves a known system (2x + y = 5, x + 3y = 10)",
   all(near(a, b, 1e-12) for a, b in zip(wp._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0]), [1.0, 3.0])))

# -------------------------------------------------------------- 3. predict
base = dict(f)
p0 = wp.predict(MODEL, base)
ok("a probability strictly between 0 and 1", p0 is not None and 0 < p0 < 1, p0)
ok("a wider home margin raises the home chance", wp.predict(MODEL, dict(base, margin=15.0)) > p0)
ok("the away QB ruled out raises it; the home QB ruled out lowers it",
   wp.predict(MODEL, dict(base, qb=1)) > p0 > wp.predict(MODEL, dict(base, qb=-1)))
ok("the same matchup at a neutral site is LESS likely for the 'home' team",
   wp.predict(MODEL, dict(base, neutral=True)) < p0)
avg = {k: MODEL["mu"][k] for k in wp.GAPS}
ok("a missing gap reads as the average game for that input, not as an extreme",
   near(wp._vector(dict(base, off=None), MODEL["mu"], MODEL["sd"])[1 + wp.GAPS.index("off")], 0.0, 0)
   and near(wp._vector(dict(base, **avg), MODEL["mu"], MODEL["sd"])[1], 0.0, 1e-12))
# `prior` has a training mean well away from zero in standard units, so a
# missing prior read as the VALUE 0 would land visibly off-centre.
far = {"mu": dict(MODEL["mu"], prior=5.0), "sd": MODEL["sd"]}
ok("  a missing gap is centred even when the input's mean is far from zero",
   wp._vector(dict(base, prior=None), far["mu"], far["sd"])[1 + wp.GAPS.index("prior")] == 0.0)
v_missing = wp._vector(dict(base, off=None, margin=None), MODEL["mu"], MODEL["sd"])
v_present = wp._vector(base, MODEL["mu"], MODEL["sd"])
ok("and 'missing' is its own column: no_epa and no_margin switch on",
   v_missing[-2:] == [1.0, 1.0] and v_present[-2:] == [0.0, 0.0], (v_missing[-2:], v_present[-2:]))
ok("the vector has one entry per column, home first", len(v_present) == len(wp.COLUMNS) and v_present[0] == 1.0)

# ----------------------------------------------------- 4. walk-forward grade
tested = wp.walk_forward(ROWS, first_test=2024)
ok("walk-forward tests exactly the seasons from first_test on",
   {r["season"] for r in tested} == {2024, 2025} and len(tested) == 543, len(tested))
card = wp.scorecard(tested)
ok("the model beats always-home on the same games (accuracy and log loss)",
   card["accuracy"] > card["always_home_accuracy"] and card["log_loss"] < card["always_home_log_loss"],
   {k: card[k] for k in ("accuracy", "always_home_accuracy", "log_loss", "always_home_log_loss")})
ok("  and loses to the closing price on them: it is a predictor, not an edge",
   card["price"]["log_loss"] < card["log_loss"] and card["price"]["accuracy"] > card["accuracy"], card["price"])
ok("  the always-home baseline is each fold's own training rate, not a global one",
   {round(r["p_home_rate"], 6) for r in tested if r["season"] == 2024}
   == {round(sum(r["home_won"] for r in ROWS if r["season"] < 2024)
             / sum(1 for r in ROWS if r["season"] < 2024), 6)})
ok("  the card carries THIS season's grade separately, with its own interval",
   card.get("current_season", {}).get("season") == 2025 and card["current_season"]["games"] == 271
   and len(card["current_season"]["minus_always_home_log_loss"]["ci95"]) == 2, card.get("current_season"))

# LEAKAGE: flip every 2025 result. 2025's predictions come from 2021-2024 only,
# so they must not move by a single digit.
flipped = [dict(r, home_won=1 - r["home_won"]) if r["season"] == 2025 else r for r in ROWS]
a = {(r["season"], r["week"], r["home"]): r["p_model"] for r in tested if r["season"] == 2025}
b = {(r["season"], r["week"], r["home"]): r["p_model"]
     for r in wp.walk_forward(flipped, first_test=2024) if r["season"] == 2025}
ok("NO LEAKAGE: flipping every 2025 result leaves every 2025 prediction unchanged", a == b and len(a) == 271)
c = {(r["season"], r["week"], r["home"]): r["p_model"]
     for r in wp.walk_forward([dict(r, home_won=1 - r["home_won"]) if r["season"] == 2023 else r for r in ROWS],
                              first_test=2024) if r["season"] == 2025}
ok("  while flipping an EARLIER season does move them (the check can fail)", a != c)

# PRICE-BLIND: the market number is on the rows for grading; it must not train.
no_price = [dict(r, p_market=None) for r in ROWS]
wrong_price = [dict(r, p_market=1.0 - r["p_market"]) if r["p_market"] is not None else r for r in ROWS]
ok("PRICE-BLIND: removing or inverting every market number leaves the fit identical",
   wp.fit(no_price) == MODEL and wp.fit(wrong_price) == MODEL)
ok("  and no feature the model reads is a price",
   not any(k in ("p_market", "odds", "home_ml", "away_ml", "spread") for k in wp.features(INPUTS)))

ok("log loss: a coin flip costs ln 2, a confident miss costs more than a hedged one",
   near(wp.log_loss(0.5, 1), 0.693147, 1e-6) and wp.log_loss(0.9, 0) > wp.log_loss(0.6, 0)
   and near(wp.log_loss(0.8, 1), wp.log_loss(0.2, 0), 1e-12))

# ----------------------------------------------------------- 5. the refusal
good = wp.build_model(ROWS)
ok("a real refit is accepted", wp.refusal(good, None) is None, wp.refusal(good, None))
ok("  and against a model built on the same number of games",
   wp.refusal(good, {"games": good["games"]}) is None)
ok("a refit on FEWER games than the model on file is refused",
   "fewer" in (wp.refusal(good, {"games": good["games"] + 1}) or ""), wp.refusal(good, {"games": good["games"] + 1}))
rng = random.Random(17)
shuffled_results = [r["home_won"] for r in ROWS]
rng.shuffle(shuffled_results)
noise = wp.build_model([dict(r, home_won=y) for r, y in zip(ROWS, shuffled_results)])
ok("a refit that is no better than always-home is refused (results shuffled across games)",
   "always-home" in (wp.refusal(noise, None) or ""), wp.refusal(noise, None))
ok("the model file carries its derivation: games, seasons, fit date, scorecard",
   good["games"] == 1355 and good["seasons"] == [2021, 2025] and good["fitted_at"]
   and good["walk_forward"]["games"] > 0 and good["columns"] == list(wp.COLUMNS))

# ---------------------------------------------------- 6. a model that is absent
with tempfile.TemporaryDirectory() as tmp:
    ok("no model file: None", wp.load_model(os.path.join(tmp, "absent.json")) is None)
    bad = os.path.join(tmp, "bad.json")
    open(bad, "w").write('{"weights": {"home": 0.2')
    ok("a half-written file: None, not an exception", wp.load_model(bad) is None)
    partial = os.path.join(tmp, "partial.json")
    json.dump({"weights": {"home": 0.2}, "mu": {}, "sd": {}}, open(partial, "w"))
    ok("a file missing columns: None", wp.load_model(partial) is None)
    whole = os.path.join(tmp, "whole.json")
    json.dump(good, open(whole, "w"))
    loaded = wp.load_model(whole)
    ok("a written model reads back and predicts the same",
       loaded is not None and near(wp.predict(loaded, base), wp.predict(good, base), 1e-12))
def _never_raises(model):
    try:
        return wp.predict(model, base) is None
    except Exception:  # noqa: BLE001 -- the failure being tested for (W9 first crashed the suite)
        return False


ok("predict with no model is None, never an exception",
   _never_raises(None) and _never_raises({}) and _never_raises({"weights": {}, "mu": {}, "sd": {}}))
try:
    blank = wp.game_block(INPUTS, {}, model={})
except Exception as exc:  # noqa: BLE001
    blank = {"home": repr(exc), "away": None, "features": None}
ok("the game block without a model: no chance, but the raw features are still recorded",
   blank["home"] is None and blank["away"] is None and blank["features"] == f, blank)
block = wp.game_block(INPUTS, {}, model=good)
ok("the game block with one: home and away sum to 1, and the fit date rides along",
   near(block["home"] + block["away"], 1.0, 1e-4) and block["fitted_at"] == good["fitted_at"], block)

# The card line: the more-likely side, its chance, and the market's for the
# SAME side (LAC @ BUF on the bets capture: BUF -340 / LAC +270).
ODDS = json.load(open(os.path.join(HERE, "bets_fixture.json")))["entities"]["2026_03_LAC_BUF"]["odds"]
d = wp.display({"home": 0.82, "away": 0.18, "fitted_at": "2026-11-10"}, "BUF", "LAC", ODDS)
ok("display: the home side when it is the likelier, with the market's chance for it",
   d["side"] == "BUF" and d["chance"] == 0.82 and near(d["market_chance"], 0.7407, 5e-4), d)
d2 = wp.display({"home": 0.30, "away": 0.70}, "BUF", "LAC", ODDS)
ok("display: the AWAY side when it is the likelier, with the market's chance for the away side",
   d2["side"] == "LAC" and d2["chance"] == 0.70 and near(d2["market_chance"], 1 - 0.7407, 5e-4), d2)
ok("display: an unpriced game still gets the pick, with no market number",
   wp.display({"home": 0.6, "away": 0.4}, "BUF", "LAC", None)["market_chance"] is None
   and wp.display({"home": 0.6, "away": 0.4}, "BUF", "LAC", {"home_ml": -340, "away_ml": None})["market_chance"] is None)
ok("display: no fitted chance, no line",
   wp.display({"home": None, "away": None, "features": f}, "BUF", "LAC", ODDS) is None
   and wp.display(None, "BUF", "LAC", ODDS) is None)

# A quiet week must not rewrite the file.
ok("a refit that found no new game is 'unchanged'", wp.unchanged(good, dict(good, fitted_at="2020-01-01")))
ok("  one more game, a new season span, or no model on file is not",
   not wp.unchanged(good, dict(good, games=good["games"] - 1))
   and not wp.unchanged(good, dict(good, seasons=[2021, 2024])) and not wp.unchanged(good, None))

# ------------------------------------------------- 7. where it is carried
NFL_SRC = open(os.path.join(_ROOT, "fetchers", "nfl.py")).read()
ok("the NFL builder attaches win_prob from the SAME inputs the score read",
   "nfl_winprob.game_block(inputs, availability)" in NFL_SRC and '"win_prob": win_prob,' in NFL_SRC)
GI = open(os.path.join(_ROOT, "generate_insights.py")).read()
ok("the games section carries the card's model line", '"model_pick": _model_pick(ent),' in GI)
ok("both allowlists carry it: the graded store and the games section",
   GI.count('"win_prob": ent.get("win_prob"),') == 2, GI.count('"win_prob": ent.get("win_prob"),'))
for name in ("signal_core.py", "nfl_signals.py"):
    ok("{} does not import the model: the Signal Score is unchanged by it".format(name),
       "nfl_winprob" not in open(os.path.join(_ROOT, name)).read())

# ------------------------------------------------------ 8. the Bets row note
FX = json.load(open(os.path.join(HERE, "bets_fixture.json")))


def buf_row(home_p):
    """The real board with win_prob ADDED to BUF's game (BUF is the home side
    and the lean; its market chance on this capture is 0.7407)."""
    ents = copy.deepcopy(FX["entities"])
    key = "2026_03_LAC_BUF"
    if home_p is not None:
        ents[key]["win_prob"] = {"home": home_p, "away": round(1 - home_p, 4)}
    board = bet_board.build(ents, CONFIG, [])
    return next(l for l in board["parlay"] + board["straight"] if l["side"] == "BUF")


plain = buf_row(None)
ok("no model on the game: no model chance and no note", plain["model_prob"] is None
   and plain["model_over_market"] is None and not any("Model" in n for n in plain["notes"]), plain["notes"])
ok("  BUF's market chance on this capture is 0.7407", near(plain["fair"], 0.7407, 5e-4), plain["fair"])
high = buf_row(0.82)
ok("model 82% against market 74%: the row carries the caution, with both numbers",
   any(n.startswith("Model 82% vs market 74%") for n in high["notes"]), high["notes"])
ok("  and the gap is recorded on the row", near(high["model_over_market"], 0.82 - plain["fair"], 1e-3))
# The line itself, from the UNROUNDED market chance the row is built from.
import parlay_boost  # noqa: E402
FAIR = parlay_boost.fair_prob(FX["entities"]["2026_03_LAC_BUF"]["odds"], True)
ok("exactly 5 points above the market: noted",
   any("Model" in n for n in buf_row(FAIR + 0.05)["notes"]), FAIR)
ok("a hair under 5 points: not noted", not any("Model" in n for n in buf_row(FAIR + 0.05 - 1e-6)["notes"]))
low = buf_row(0.60)
ok("model BELOW the market (60% vs 74%): no note -- the caution is one-sided",
   not any("Model" in n for n in low["notes"]) and low["model_over_market"] < 0, low["notes"])
ok("the note never changes the row's price, chance or place on the board",
   (high["decimal"], high["fair"], high["score"]) == (plain["decimal"], plain["fair"], plain["score"]))
ok("the threshold is the measured one", wp.DISAGREE_MIN == 0.05)

print("nfl winprob: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nfl winprob: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
