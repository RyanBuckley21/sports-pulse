#!/usr/bin/env python3
"""NBA moneyline backtest, part 1: MEASURE the candidate signals. No weights.

    python3 nba_backtest.py                                   # the captured dataset
    python3 nba_backtest.py --dataset PATH --seasons 2025 2026

Read-only, offline by default: it reads tools/verify/nba_seasons_2024_2026.json
(3,690 decided games, 2023-24..2025-26, built by fetchers.nba.fetch_season_games
in a session with ESPN access -- this sandbox's proxy blocks ESPN). It writes
nothing: not config.yaml, not the ledger, not a backtest file. Its output is a
report that decides what nba_signals.py may contain.

WHAT IS MEASURED, and why each is a candidate. Only what the dataset itself
supports -- final scores, Eastern dates, home/away, neutral site -- so every
signal here can be rebuilt live from the same feed with no new source:

  season_margin   each team's points margin per game this season, BEFORE this
                  game's date. The NBA counterpart of NFL's scoring margin.
  recent_margin   the same over each team's last RECENT_WINDOW games.
  rest_diff       days since each team's previous game, capped at REST_CAP,
                  home minus away. A back-to-back is the measurable case.
  prior_margin    last season's margin per game (build_prior_season_margin):
                  the cold-start tier, measured on its own because it is only
                  ever used when in-season form is too thin.

POINT-IN-TIME: a game on Eastern date D sees only games with slate_date < D in
the SAME season, never its own result. (Form is appended after the whole date
is recorded, though no team plays twice in a night, so same-night ordering
cannot leak by itself: a sabotage of that ordering alone passed test_nba, and
was replaced by the one that matters -- a game seeing its own result.)

THE BASELINE IS NAMED FIRST (CLAUDE.md "Add NBA"). Always-home wins 0.5473 of
the 3,678 true-home games pooled over 2023-26. Every accuracy below is reported
ON THE SAME GAMES as always-home, with a PAIRED bootstrap interval on the
difference -- a signal that only matches the home side is not a result. The
"always the favourite" baseline does not exist historically (no NBA prices on
ESPN after a game completes), so it is not reported.

STATISTICS: point-biserial r of the signed home-minus-away gap against the home
win, and accuracy of picking the side the gap favours (gap 0 = no pick), each
with a 95% bootstrap interval, 4,000 resamples, seed 17 -- the procedure every
NBA figure in CLAUDE.md uses. Neutral-site games are kept for r and accuracy
(the signals are team-relative) but excluded from the always-home comparison.
"""

import argparse
import datetime
import json
import os
import random
import sys

import nba_signals
from fetchers import nba

DATASET = os.path.join("tools", "verify", "nba_seasons_2024_2026.json")
N_BOOT, SEED = 4000, 17
RECENT_WINDOW = 10      # NFL uses its own window; this one is a candidate, reported with coverage
REST_CAP = 4            # rest beyond four days is "rested"; uncapped gaps are All-Star breaks
MIN_GAMES = (1, 5, 10, 20)   # season_margin floors reported, not chosen here


def load(path, seasons=None):
    """{season: [game dict]} from the dataset, decided games only (it holds
    nothing else), sorted by slate date."""
    raw = json.load(open(path))
    out = {}
    for season, rows in raw.items():
        if season.startswith("_") or (seasons and int(season) not in seasons):
            continue
        games = [{"date": r[0], "away": r[1], "home": r[2], "away_score": r[3],
                  "home_score": r[4], "neutral": bool(r[5])} for r in rows]
        out[int(season)] = sorted(games, key=lambda g: g["date"])
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def point_in_time(games, prior=None):
    """One record per game: each team's form from strictly earlier dates of the
    same season, plus last season's margin, plus the outcome."""
    history = {}            # team -> [(date, margin)]
    records = []
    by_date = {}
    for g in games:
        by_date.setdefault(g["date"], []).append(g)
    for date in sorted(by_date):
        day = datetime.date.fromisoformat(date)
        for g in by_date[date]:
            rec = {"date": date, "home": g["home"], "away": g["away"], "neutral": g["neutral"],
                   "home_win": g["home_score"] > g["away_score"],
                   "home_margin": g["home_score"] - g["away_score"]}
            for side in ("home", "away"):
                h = history.get(g[side], [])
                rec[side + "_n"] = len(h)
                rec[side + "_season"] = _mean([m for _, m in h])
                rec[side + "_recent"] = (_mean([m for _, m in h[-RECENT_WINDOW:]])
                                         if len(h) >= RECENT_WINDOW else None)
                rec[side + "_rest"] = (min((day - datetime.date.fromisoformat(h[-1][0])).days,
                                           REST_CAP) if h else None)
                p = (prior or {}).get(g[side])
                rec[side + "_prior"] = p["margin"] if p else None
            records.append(rec)
        for g in by_date[date]:           # only AFTER every game of the date is recorded
            d = g["home_score"] - g["away_score"]
            history.setdefault(g["home"], []).append((date, d))
            history.setdefault(g["away"], []).append((date, -d))
    return records


SIGNALS = {
    "season_margin": lambda r, k: (None if min(r["home_n"], r["away_n"]) < k
                                   else r["home_season"] - r["away_season"]),
    "recent_margin": lambda r, k: (None if r["home_recent"] is None or r["away_recent"] is None
                                   else r["home_recent"] - r["away_recent"]),
    "rest_diff": lambda r, k: (None if r["home_rest"] is None or r["away_rest"] is None
                               else r["home_rest"] - r["away_rest"]),
    "prior_margin": lambda r, k: (None if r["home_prior"] is None or r["away_prior"] is None
                                  else r["home_prior"] - r["away_prior"]),
}


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else None


def _boot(n, stat):
    rng = random.Random(SEED)
    vals = sorted(v for v in (stat([rng.randrange(n) for _ in range(n)]) for _ in range(N_BOOT))
                  if v is not None)
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def measure(records, name, floor=1):
    """r, accuracy, and accuracy minus always-home on the same true-home games."""
    pts = [(SIGNALS[name](r, floor), r) for r in records]
    pts = [(x, r) for x, r in pts if x is not None]
    if len(pts) < 30:
        return None
    xs = [x for x, _ in pts]
    ys = [1.0 if r["home_win"] else 0.0 for _, r in pts]
    r_val = _pearson(xs, ys)
    r_ci = _boot(len(xs), lambda ix: _pearson([xs[i] for i in ix], [ys[i] for i in ix]))
    picks = [(x > 0) == r["home_win"] for x, r in pts if x != 0]
    acc = sum(picks) / len(picks)
    acc_ci = _boot(len(picks), lambda ix: sum(picks[i] for i in ix) / len(ix))
    paired = [((x > 0) == r["home_win"]) - (1 if r["home_win"] else 0)
              for x, r in pts if x != 0 and not r["neutral"]]
    home = [r["home_win"] for x, r in pts if x != 0 and not r["neutral"]]
    diff = sum(paired) / len(paired)
    diff_ci = _boot(len(paired), lambda ix: sum(paired[i] for i in ix) / len(ix))
    return {"signal": name, "floor": floor, "n": len(xs), "picks": len(picks),
            "r": r_val, "r_ci": r_ci, "acc": acc, "acc_ci": acc_ci,
            "home_rate": sum(home) / len(home), "vs_home": diff, "vs_home_ci": diff_ci,
            "coverage": len(xs) / len(records)}


def all_records(by_season):
    """point_in_time over every season, each record tagged with its season and
    carrying last season's margin where the dataset holds that season."""
    records = []
    for s in sorted(by_season):
        prior = (nba.build_prior_season_margin(
                    [dict(g, completed=True) for g in by_season[s - 1]]) if (s - 1) in by_season else None)
        recs = point_in_time(by_season[s], prior)
        for r in recs:
            r["season"] = s
        records += recs
    return records


def report(by_season):
    seasons = sorted(by_season)
    records = all_records(by_season)
    true_home = [r for r in records if not r["neutral"]]
    base = sum(r["home_win"] for r in true_home) / len(true_home)
    print("NBA backtest -- seasons {}, {} decided games, {} at a true home venue".format(
        ", ".join(str(s) for s in seasons), len(records), len(true_home)))
    print("Baseline: always-home {:.4f} on the {} true-home games\n".format(base, len(true_home)))
    hdr = "{:<14} {:>5} {:>6} {:>5}  {:>22}  {:>22}  {:>6}  {:>26}"
    print(hdr.format("signal", "floor", "n", "cov", "r [95%]", "accuracy [95%]", "home",
                     "accuracy - always-home"))
    rows = []
    for name in SIGNALS:
        for floor in (MIN_GAMES if name == "season_margin" else (1,)):
            m = measure(records, name, floor)
            if not m:
                print("{:<14} {:>5}  too few games".format(name, floor))
                continue
            rows.append(m)
            print("{:<14} {:>5} {:>6} {:>4.0%}  {:+.3f} [{:+.3f},{:+.3f}]  {:.3f} [{:.3f},{:.3f}]  "
                  "{:.3f}  {:+.3f} [{:+.3f},{:+.3f}] {}".format(
                      name, floor, m["n"], m["coverage"], m["r"], m["r_ci"][0], m["r_ci"][1],
                      m["acc"], m["acc_ci"][0], m["acc_ci"][1], m["home_rate"],
                      m["vs_home"], m["vs_home_ci"][0], m["vs_home_ci"][1],
                      "beats home" if m["vs_home_ci"][0] > 0 else
                      ("WORSE than home" if m["vs_home_ci"][1] < 0 else "no better than home")))
    return rows


# ---------------------------------------------------------------------------
# PART 2 (2026-11-11): from measured signals to the numbers config.yaml ships.
#
#   python3 nba_backtest.py --fit
#
# FIT ON THE FIRST TWO SEASONS, TEST ON THE THIRD. Scales, home shifts and the
# threshold come from 2023-24 and 2024-25 only; 2025-26 is scored once, with
# those numbers, and that is the result reported. Nothing is chosen by looking
# at the test season.
#
# Every rule here is one this repo already used for NFL and CFB, on purpose:
#   scale       the standard deviation of the tier's own raw gap
#               (nfl_backtest.derive_calibration);
#   home shift  intercept / slope of OLS, home margin on the tier's gap --
#               "FITTED, NOT SEARCHED" (config.yaml's nfl home_field block);
#   threshold   the LARGEST candidate that still names a side in at least a
#               third of games (nfl_backtest._pick_threshold), read on the
#               FIT seasons.
SEASON_FLOOR = 5          # games both clubs need for a season margin. Measured
                          # in part 1: accuracy is flat across floors 5/10/20
                          # (0.657/0.659/0.658) and only the baseline moves.
THRESHOLDS = (0, 10, 20, 30, 40, 50, 60, 70)
COVERAGE_FLOOR = 1.0 / 3.0


def tier_gap(rec, name):
    """The raw home-minus-away gap a tier scores on, or None when it has none."""
    if name == "season_margin":
        if min(rec["home_n"], rec["away_n"]) < SEASON_FLOOR:
            return None
        return rec["home_season"] - rec["away_season"]
    if rec["home_prior"] is None or rec["away_prior"] is None:
        return None
    return rec["home_prior"] - rec["away_prior"]


def _ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return my - slope * mx, slope


def fit_tiers(records):
    """{tier: {n, scale, home_edge, slope, shift}} from TRUE-HOME games only:
    a neutral-site game has no home edge to measure and gets no shift."""
    out = {}
    for name in nba_signals.SIGNAL_SPECS:
        pts = [(tier_gap(r, name), r["home_margin"]) for r in records if not r["neutral"]]
        pts = [(x, y) for x, y in pts if x is not None]
        if len(pts) < 30:
            continue
        xs, ys = [x for x, _ in pts], [y for _, y in pts]
        mean = sum(xs) / len(xs)
        scale = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5
        edge, slope = _ols(xs, ys)
        out[name] = {"n": len(xs), "scale": scale, "home_edge": edge, "slope": slope,
                     "shift": edge / slope}
    return out


def candidate_config(fit, threshold):
    """A config dict shaped like config.yaml's, from a fit and one threshold."""
    return {"betting_signals": {"nba": {
        "min_threshold": threshold, "standout_threshold": threshold,
        "scales": {nba_signals.SIGNAL_SPECS[k]["scale_key"]: round(v["scale"], 4)
                   for k, v in fit.items()},
        "home_field": {k: round(v["shift"], 3) for k, v in fit.items()},
        "bet_types": {"moneyline": {k: 1.0 for k in fit}}}}}


def score_record(rec, config):
    """The live scorer's own answer for a historical game: the SAME
    nba_signals.score_game the site runs, on the inputs it would have had."""
    season_ok = min(rec["home_n"], rec["away_n"]) >= SEASON_FLOOR
    inputs = nba_signals.build_inputs(
        rec["away"], rec["home"],
        away_season_margin=rec["away_season"] if season_ok else None,
        home_season_margin=rec["home_season"] if season_ok else None,
        away_prior_margin=rec["away_prior"], home_prior_margin=rec["home_prior"],
        neutral_site=rec["neutral"])
    return nba_signals.score_game(config, "nba", inputs).get("moneyline") or {}


def graded(records, config):
    """[(record, entry, picked_home, hit)] for every game the config names a
    side in."""
    rows = []
    for r in records:
        e = score_record(r, config)
        if e.get("side") in (r["home"], r["away"]):
            home = e["side"] == r["home"]
            rows.append((r, e, home, home == r["home_win"]))
    return rows


def _rate(flags):
    return sum(flags) / len(flags) if flags else None


def _line(label, rows, records_n=None):
    """One result line: hit rate, always-home on THE SAME GAMES, and the paired
    difference with its interval. Neutral-site picks have no 'home' to pick, so
    the paired comparison is on true-home games and says how many."""
    if len(rows) < 30:
        print("  {:<34} n={:<5} (too few to report)".format(label, len(rows)))
        return
    hits = [h for _, _, _, h in rows]
    th = [(h, r["home_win"]) for r, _, _, h in rows if not r["neutral"]]
    paired = [int(h) - int(hw) for h, hw in th]
    lo, hi = _boot(len(paired), lambda ix: sum(paired[i] for i in ix) / len(ix))
    cov = "" if records_n is None else "  names a side in {:.0%}".format(len(rows) / records_n)
    print("  {:<34} n={:<5} hit {:.4f}  always-home {:.4f} on the same {}  "
          "{:+.1f}pp [{:+.1f}, {:+.1f}]{}".format(
              label, len(rows), _rate(hits), _rate([hw for _, hw in th]), len(th),
              100 * sum(paired) / len(paired), 100 * lo, 100 * hi, cov))


def fit_report(by_season, test_season=None):
    records = all_records(by_season)
    seasons = sorted(by_season)
    test_season = test_season or seasons[-1]
    fit_recs = [r for r in records if r["season"] < test_season]
    test_recs = [r for r in records if r["season"] == test_season]
    print("NBA fit -- fitted on seasons {} ({} games), tested on {} ({} games)\n".format(
        ", ".join(str(s) for s in seasons if s < test_season), len(fit_recs),
        test_season, len(test_recs)))
    fit = fit_tiers(fit_recs)
    for name, v in fit.items():
        print("  {:<14} n={:<5} scale (sd of gap) {:.4f}   home edge {:+.3f} pts / slope {:.4f} "
              "= shift {:+.3f}".format(name, v["n"], v["scale"], v["home_edge"], v["slope"], v["shift"]))

    print("\nthreshold, on the FIT seasons only (rule: the largest that still names a side in a third of games)")
    chosen = THRESHOLDS[0]
    for t in THRESHOLDS:
        rows = graded(fit_recs, candidate_config(fit, t))
        cov = len(rows) / len(fit_recs)
        if cov >= COVERAGE_FLOOR:
            chosen = t
        print("  >= {:<3} names a side in {:>5.1%} ({:>4} games)  hit {:.4f}".format(
            t, cov, len(rows), _rate([h for _, _, _, h in rows]) or 0.0))
    config = candidate_config(fit, chosen)
    print("  chosen: {}".format(chosen))

    print("\nHELD-OUT season {}, scored once with the numbers above".format(test_season))
    rows = graded(test_recs, config)
    _line("all picks", rows, len(test_recs))
    for tier in nba_signals.SIGNAL_SPECS:
        _line("  tier " + tier, [x for x in rows if x[1].get("tier") == tier])
    _line("  home leans", [x for x in rows if x[2]])
    _line("  road leans", [x for x in rows if not x[2]])
    for lo, hi in ((0, 60), (60, 75), (75, 90), (90, 101)):
        lo = max(lo, chosen)              # nothing under the threshold is a pick
        if hi > lo:
            _line("  score {}-{}".format(lo, hi - 1), [x for x in rows if lo <= x[1]["score"] < hi])
    every = graded(test_recs, candidate_config(fit, 0))
    _line("every game with a lean (threshold 0)", every, len(test_recs))
    cold = [x for x in every if min(x[0]["home_n"], x[0]["away_n"]) < SEASON_FLOOR]
    _line("  cold start only (a club under {} games)".format(SEASON_FLOOR), cold)

    block = config["betting_signals"]["nba"]
    print("\nconfig.yaml  betting_signals.nba:")
    print("  min_threshold: {}\n  standout_threshold: {}".format(chosen, chosen))
    print("  scales: {}".format(block["scales"]))
    print("  home_field: {}".format(block["home_field"]))
    print("  bet_types: {}".format(block["bet_types"]))
    return fit, chosen, config


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", default=DATASET)
    p.add_argument("--seasons", type=int, nargs="*")
    p.add_argument("--fit", action="store_true",
                   help="fit scales, home shifts and the threshold on all but the last "
                        "season and test on the last (part 2)")
    a = p.parse_args(argv)
    if a.fit:
        fit_report(load(a.dataset, a.seasons))
    else:
        report(load(a.dataset, a.seasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
