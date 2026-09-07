"""How well does implied_total.py's own point estimate track reality?

    python3 mlb_estimate_calibration.py

WHY THIS EXISTS. betting_signals.py / implied_total.py have never been backtested
against a real market price for MLB the way nfl_odds_backtest.py did for NFL --
no free, keyless, currently-updated source of real 2026-season MLB odds is
reachable from this environment (checked directly; the standard historical-odds
archive is blocked by network policy, and the only pre-scraped mirror found stops
in August 2025). Real closing lines are therefore not an option right now.

This is the fallback: data/signal_report_history.jsonl already grades some
totals picks against implied_total's OWN point estimate (basis="estimate",
stamped on the pick by generate_insights._attach_estimates when it was made,
compared later to the real final by signal_report.grade). That is a genuinely
different and weaker question than "did the pick beat the market" -- see WHAT
THIS CANNOT TELL YOU -- but it needs no new data source, and it can be measured
today.

WHAT IT MEASURES. Every basis="estimate" row for game_total, first_five_total,
and team_total, joined to data/training/mlb_features.jsonl on (gamePk, date) for
venue (all rows) and combined starter ERA (schema_version 4 rows only -- earlier
schema versions store a differently-scaled starter ERA; see training_capture.py's
SCHEMA_VERSION comment. Mixing scales into one "matchup quality" bucket would be
a self-inflicted error, so that one cut is restricted to v4 rather than corrected
for). Reports, overall and by cut: mean bias (actual - point; positive means the
model estimate ran LOW), MAE, Pearson r, and a bootstrap CI on bias -- plus hit
rate, included for continuity but caveated below.

WHAT THIS CANNOT TELL YOU.
  - Grading a total against ITS OWN point estimate makes hit rate close to
    tautological: a merely well-CENTERED estimate lands near 50% over/under by
    construction, with or without any real game-specific predictive content.
    Bias, MAE, and correlation are the numbers that actually say something here;
    hit rate is reported only because earlier reporting in this project leaned on
    it and dropping it silently would look like hiding a number, not because it
    is informative on its own for this kind of self-referential grading.
  - This is NOT the full 2026 season. `point` was first attached to a graded
    pick on 2026-07-28 -- nothing before that carries an estimate to grade
    against. This script reports whatever window actually has rows, and prints
    that window explicitly so it is never silently mistaken for opening day
    forward.
  - The three markets pool to well under 100 rows total, and by-park / by-quality
    / by-week cuts split that further. Every cut prints its own n; nothing under
    8 rows gets a bootstrap CI, and nothing is asserted as a real effect below
    that floor -- it is reported as a count, not a rate.

Network: none. Everything read is already on disk.
"""

import collections
import datetime
import json
import math
import random
import statistics
import sys

SIGNAL_REPORT_PATH = "data/signal_report_history.jsonl"
FEATURES_PATH = "data/training/mlb_features.jsonl"
TOTAL_BET_TYPES = ("game_total", "first_five_total", "team_total")
# Same procedure and seed as nfl_odds_backtest.bootstrap_mean_ci, for consistency
# across this repo's calibration scripts.
BOOTSTRAP_ITERS = 4000
BOOTSTRAP_SEED = 17
MIN_FOR_CI = 8  # below this, a bootstrap CI is more noise than signal; report n only


def _read_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def bootstrap_mean_ci(values, iters=BOOTSTRAP_ITERS, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def wilson_ci(h, n, z=1.96):
    if n == 0:
        return None, None
    p = h / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return 100 * (center - margin), 100 * (center + margin)


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _actual_value(bt, side, away, home, observed):
    """The real number the pick's `point` estimate should be compared to."""
    if bt == "game_total":
        return (observed.get("away_score") or 0) + (observed.get("home_score") or 0)
    if bt == "first_five_total":
        return (observed.get("f5_away") or 0) + (observed.get("f5_home") or 0)
    if bt == "team_total":
        abbr = (side or "").split(" ")[0]
        if abbr == away:
            return observed.get("away_score")
        if abbr == home:
            return observed.get("home_score")
    return None


def load_estimate_rows(sr_path=SIGNAL_REPORT_PATH, feat_path=FEATURES_PATH):
    """Every basis='estimate' graded totals pick, joined to its feature row.

    Join key is (str(gamePk), date) -- the same convention used throughout this
    project's stores. gamePk is a string in signal_report_history.jsonl and an
    int in mlb_features.jsonl, so the string side is normalized here."""
    sr = _read_jsonl(sr_path)
    feat = _read_jsonl(feat_path)
    feat_by_key = {(str(r.get("gamePk")), r.get("date")): r for r in feat}

    rows = []
    for r in sr:
        if r.get("bet_type") not in TOTAL_BET_TYPES or r.get("basis") != "estimate":
            continue
        point = r.get("point")
        if point is None:
            continue
        observed = r.get("observed") or {}
        actual = _actual_value(r["bet_type"], r.get("side"), r.get("away"), r.get("home"), observed)
        if actual is None:
            continue
        f = feat_by_key.get((str(r.get("gamePk")), r.get("date")))
        combined_era = None
        if f and f.get("schema_version") == 4:
            feats = f.get("features") or {}
            a_era, h_era = feats.get("away_starter_era"), feats.get("home_starter_era")
            if a_era is not None and h_era is not None:
                combined_era = (a_era + h_era) / 2.0
        rows.append({
            "bet_type": r["bet_type"], "date": r["date"], "point": point,
            "actual": actual, "bias": actual - point,
            "venue": (f or {}).get("venue"), "combined_era": combined_era,
            "side": r.get("side"),
        })
    return rows


def summarize(label, rows):
    n = len(rows)
    if n == 0:
        return
    biases = [r["bias"] for r in rows]
    mean_bias = statistics.fmean(biases)
    mae = statistics.fmean(abs(b) for b in biases)
    r_val = pearson([r["point"] for r in rows], [r["actual"] for r in rows])
    if n >= MIN_FOR_CI:
        lo, hi = bootstrap_mean_ci(biases)
        ci_txt = "95% CI [{:+.2f}, {:+.2f}]".format(lo, hi)
    else:
        ci_txt = "(n<{}, no CI)".format(MIN_FOR_CI)
    r_txt = "{:+.2f}".format(r_val) if r_val is not None else "n/a"
    print("  {:<30} n={:<4} mean_bias={:+.2f} runs  {:<24} MAE={:.2f}  r={}"
          .format(label, n, mean_bias, ci_txt, mae, r_txt))


def hit_rate_line(label, rows):
    n = len(rows)
    if n == 0:
        return
    hits = sum(1 for r in rows if (r["actual"] > r["point"]) == (str(r["side"]).split(" ")[-1] == "Over"))
    pushes = sum(1 for r in rows if r["actual"] == r["point"])
    decided = n - pushes
    if decided == 0:
        print("  {:<30} n={:<4} all pushes".format(label, n))
        return
    lo, hi = wilson_ci(hits, decided)
    verdict = "contains 50%" if lo <= 50 <= hi else "excludes 50%"
    print("  {:<30} n={:<4} hit {:5.1f}% of {:<3} decided  95% CI [{:5.1f}%, {:5.1f}%]  {}"
          .format(label, n, 100 * hits / decided, decided, lo, hi, verdict))


def _week_of(date_str):
    dt = datetime.date.fromisoformat(date_str)
    return (dt - datetime.timedelta(days=dt.weekday())).isoformat()


def main():
    rows = load_estimate_rows()
    if not rows:
        print("mlb-estimate-calibration: no basis='estimate' rows found.")
        return 1

    dates = sorted(set(r["date"] for r in rows))
    print("mlb-estimate-calibration: {} rows, {} distinct dates, {} -> {}\n"
          .format(len(rows), len(dates), dates[0], dates[-1]))
    print("NOT the full season: implied_total's point estimate was first attached")
    print("to a graded pick on 2026-07-28. This is the entire window that has one.\n")
    print("Hit rate is reported for continuity but is close to tautological here --")
    print("grading a total against its OWN point estimate lands near 50% by")
    print("construction for a merely well-centered estimate, informative or not.")
    print("bias / MAE / r are the numbers that actually say something.\n")

    print("=== overall, by market (bias = actual - estimate; + means estimate ran LOW) ===")
    for bt in TOTAL_BET_TYPES:
        summarize(bt, [r for r in rows if r["bet_type"] == bt])
    summarize("ALL THREE POOLED", rows)
    print()

    print("=== hit rate (secondary -- see caveat above) ===")
    for bt in TOTAL_BET_TYPES:
        hit_rate_line(bt, [r for r in rows if r["bet_type"] == bt])
    hit_rate_line("ALL THREE POOLED", rows)
    print()

    print("=== by park (venue), team_total only -- the one market with enough rows to cut ===")
    by_venue = collections.defaultdict(list)
    for r in rows:
        if r["bet_type"] == "team_total" and r["venue"]:
            by_venue[r["venue"]].append(r)
    for venue, vr in sorted(by_venue.items(), key=lambda kv: -len(kv[1])):
        summarize(venue, vr)
    print()

    print("=== by matchup quality: combined starter ERA, schema_version==4 rows only ===")
    qrows = [r for r in rows if r["combined_era"] is not None]
    print("  ({} of {} rows carry a v4 combined-ERA value to bucket on)\n".format(len(qrows), len(rows)))
    if qrows:
        eras = sorted(r["combined_era"] for r in qrows)

        def pct(p):
            return eras[min(len(eras) - 1, int(p * len(eras)))]

        t1, t2 = pct(1 / 3), pct(2 / 3)
        print("  tertile cut points: <= {:.2f} / <= {:.2f} / above {:.2f}".format(t1, t2, t2))
        summarize("strong pitching (low ERA)", [r for r in qrows if r["combined_era"] <= t1])
        summarize("average pitching", [r for r in qrows if t1 < r["combined_era"] <= t2])
        summarize("weak pitching (high ERA)", [r for r in qrows if r["combined_era"] > t2])
    print()

    print("=== bias trend by week ===")
    by_week = collections.defaultdict(list)
    for r in rows:
        by_week[_week_of(r["date"])].append(r)
    for wk in sorted(by_week):
        summarize(wk, by_week[wk])

    return 0


if __name__ == "__main__":
    sys.exit(main())
