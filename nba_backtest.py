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


def report(by_season):
    records = []
    seasons = sorted(by_season)
    for s in seasons:
        prior = (nba.build_prior_season_margin(
                    [dict(g, completed=True) for g in by_season[s - 1]]) if (s - 1) in by_season else None)
        recs = point_in_time(by_season[s], prior)
        for r in recs:
            r["season"] = s
        records += recs
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", default=DATASET)
    p.add_argument("--seasons", type=int, nargs="*")
    a = p.parse_args(argv)
    report(load(a.dataset, a.seasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
