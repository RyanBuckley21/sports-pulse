#!/usr/bin/env python3
"""NFL moneyline backtest + weight calibration against real completed seasons.

    python3 nfl_backtest.py                            # 2021-2025, full calibration
    python3 nfl_backtest.py --seasons 2023 2024 2025    # smaller window
    python3 nfl_backtest.py --min-threshold 20 --standout-threshold 20  # override the auto-picked thresholds

Mirrors backtest_season.py's role for MLB (a standalone, read-mostly replay of
real history against the live scoring path, writing its own output file,
never touching the live ledger or config.yaml) but is its OWN script rather
than an nfl-aware branch inside backtest_season.py. That script's grading
layer (signal_report.fetch_slate/is_final/grade, and build_pick_rows's own
observed_facts() call) is built entirely around MLB's StatsAPI schedule/
linescore JSON shape -- an NFL schedule row has no "status"/"linescore"
object at all, so none of that grades correctly against nflverse data, and
there is nothing sport-generic left to share once collect_picks (already
sport-generic since the precursor PR) is factored out. nflverse's schedule
CSV already carries final scores directly, so NFL grading is genuinely
simpler than MLB's, not a variant of it -- see grade_moneyline below.

This script does two things a plain "replay history through the live
pipeline" tool wouldn't:

  1. WEIGHT CALIBRATION -- measures each of nfl_signals.SIGNAL_SPECS' raw
     predictive validity (point-biserial r against the real home-win outcome,
     with a bootstrap 95% CI) and derives reliability-proportional weights
     from it, mirroring the methodology config.yaml's mlb block documents
     for its own bet_types weights (and, like that history, DROPPING a
     signal outright -- not shrinking it -- when its CI spans zero, the same
     treatment season_series got there).
  2. GRADING under those newly calibrated weights, to report a real win rate
     -- not just correlation numbers in isolation.

Point-in-time discipline: every signal for a game in week W uses ONLY
nflverse rows from weeks < W of the SAME season -- enforced by
fetchers.nfl.build_team_form / build_scoring_margins / get_starting_qb,
which this script calls DIRECTLY rather than reimplementing the cutoff (see
collect_game_records). Preseason: nflverse's schedule (games.csv) and team
stats (stats_team_week) both simply do not carry preseason rows at all --
verified live while building this (game_type values across 2021-2025 are
only REG/WC/DIV/CON/SB; team_stats' season_type values are only REG/POST),
so there is no preseason data available to leak into a Week 1 number in the
first place. GRADED_TYPES is still an explicit filter, not an assumption
resting on that -- defense in depth against an upstream schema change.

Availability (QB-out) is measured OUT of the reliability pass -- off_epa's
raw correlation is measured across every game with data, unconditionally,
the same way a real Week-6 game where the starter got hurt in Week 5 would
naturally show up as noisier signal rather than being excluded. The override
IS applied during the final grading pass, via nfl_signals.score_game itself
-- exactly as production would score it.

Nothing here writes to config.yaml. Output is a report (stdout) plus a
backtest-only JSONL (data/nfl_backtest_2021_2025.jsonl by default), sport-
tagged "nfl", separate from data/signal_report_history.jsonl exactly as
backtest_season.py's own data/backtest_2026.jsonl is kept separate from it.
"""

import argparse
import random
import statistics
import sys
import time

import requests

import nfl_signals
import signal_report
from fetchers import nfl

DEFAULT_SEASONS = list(range(2021, 2026))  # 2021-2025: the current 17-game/32-team era
POSTSEASON_TYPES = {"WC", "DIV", "CON", "SB"}
GRADED_TYPES = {"REG"} | POSTSEASON_TYPES  # explicit; see module docstring on why PRE cannot leak in anyway

OUTPUT_PATH = "data/nfl_backtest_2021_2025.jsonl"
SOURCE = "backtest:fetchers.nfl.build_team_form+nfl_signals.score_game"
SPORT_KEY = "nfl"

N_BOOT_DEFAULT = 2000
BOOT_SEED = 20260810  # fixed, so a re-run reproduces the same CI rather than jittering

# Manually excluded from weighting despite clearing the CI-based drop rule:
# a real decision made from the first 2021-2025 run's pairwise-correlation
# table (r=0.71 with off_epa, 0.60 with def_epa_allowed, 0.50 with
# turnover_diff), not a statistical result -- reliability-proportional
# weighting assumes each signal is independent evidence, and scoring_margin
# measurably is not: it is largely a restatement of what off_epa/def_epa
# already capture, differently aggregated. See derive_calibration's
# docstring for how this is kept distinct from rest_diff's CI-based drop.
DEFAULT_EXCLUDED = {"scoring_margin"}


# --------------------------------------------------------------------------- #
# Data collection -- point-in-time signal inputs for every graded game.
# --------------------------------------------------------------------------- #

def collect_game_records(session, seasons):
    """One record per graded (regular-season + postseason) game across
    `seasons`: point-in-time signal inputs (nfl_signals.build_inputs' raw
    dict, built from the SAME fetchers.nfl functions the live path calls),
    QB availability, and the real outcome.

    Fetch cost: ONE schedule fetch total (covers every season in one file),
    plus one team_stats and one injuries fetch PER SEASON -- not per game or
    per date, unlike backtest_season.py's per-date MLB schedule calls, since
    nflverse's season-level files make that unnecessary.
    """
    schedule_all = nfl.get_schedule(session)
    by_season_schedule = {s: [r for r in schedule_all if r.get("season") == str(s)] for s in seasons}
    team_stats_by_season = {s: nfl.get_team_stats(session, s) for s in seasons}
    injuries_by_season = {s: nfl.get_injuries(session, s) for s in seasons}

    records = []
    skipped = 0
    for season in seasons:
        schedule = by_season_schedule[season]
        team_stats = team_stats_by_season[season]
        injuries = injuries_by_season[season]
        print("nfl_backtest: {} -- {} schedule rows, {} team_stats rows, {} injury rows"
              .format(season, len(schedule), len(team_stats), len(injuries)))

        for g in schedule:
            game_type = g.get("game_type")
            if game_type not in GRADED_TYPES:
                continue
            a_score, h_score = g.get("away_score"), g.get("home_score")
            if not a_score or not h_score:
                continue  # unplayed -- shouldn't happen for a completed season, defensive only
            try:
                week = int(g["week"])
            except (TypeError, ValueError, KeyError):
                skipped += 1
                continue

            away, home = g["away_team"], g["home_team"]
            # Same three point-in-time functions fetchers.nfl._build_one_game
            # calls for the live path -- called directly here rather than
            # through build_game_entities' network-fetching wrapper, since
            # this script already holds the season's data in memory.
            form = nfl.build_team_form(team_stats, week)
            margins = nfl.build_scoring_margins(schedule, week)
            away_form, home_form = form.get(away, {}), form.get(home, {})

            away_qb_id, _ = nfl.get_starting_qb(schedule, away, week)
            home_qb_id, _ = nfl.get_starting_qb(schedule, home, week)
            availability = {
                "away_qb_out": nfl.qb_out(injuries, away_qb_id, week),
                "home_qb_out": nfl.qb_out(injuries, home_qb_id, week),
            }

            inputs = nfl_signals.build_inputs(
                away_abbr=away, home_abbr=home,
                away_off_epa=away_form.get("off_epa"), home_off_epa=home_form.get("off_epa"),
                away_def_epa_allowed=away_form.get("def_epa_allowed"), home_def_epa_allowed=home_form.get("def_epa_allowed"),
                away_turnover_diff=away_form.get("turnover_diff"), home_turnover_diff=home_form.get("turnover_diff"),
                away_scoring_margin=margins.get(away), home_scoring_margin=margins.get(home),
                away_rest=g.get("away_rest"), home_rest=g.get("home_rest"),
            )
            a_f, h_f = float(a_score), float(h_score)
            home_win = None if a_f == h_f else (h_f > a_f)  # None -- tie, excluded from win/loss correlation and PUSHed when graded
            records.append({
                "game_id": g["game_id"], "season": season, "week": week,
                "game_type": game_type, "postseason": game_type in POSTSEASON_TYPES,
                "away": away, "home": home,
                "away_score": a_f, "home_score": h_f, "home_win": home_win,
                "inputs": inputs, "availability": availability,
            })
    if skipped:
        print("nfl_backtest: {} schedule rows skipped (unparseable week)".format(skipped))
    return records


# --------------------------------------------------------------------------- #
# Reliability measurement -- point-biserial r, bootstrap CI, per signal.
# --------------------------------------------------------------------------- #

def _signed_gap(home_val, away_val, favors):
    """Direction-corrected raw gap: positive always means "favors home",
    matching nfl_signals._paired's sign convention but WITHOUT the tanh
    squash or the config scale -- reliability measurement must not be
    conflated with the (also being recalibrated here) scale choice; a tanh
    already-saturated near +/-1 would compress away exactly the variation a
    correlation needs to read."""
    if home_val is None or away_val is None:
        return None
    d = home_val - away_val
    return d if favors == "higher" else -d


def _pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx * syy) ** 0.5


def _bootstrap_ci(xs, ys, n_boot, seed):
    """95% bootstrap CI on Pearson r via resampling WITH replacement --
    stdlib only (no scipy/numpy dependency), mirroring the CIs already
    reported in config.yaml's mlb block (e.g. season_series' "95% CI
    [-0.083, +0.042]") in kind, not literally the same procedure that
    produced those (undocumented here)."""
    rng = random.Random(seed)
    n = len(xs)
    rs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = _pearson_r([xs[i] for i in idx], [ys[i] for i in idx])
        if r is not None:
            rs.append(r)
    if not rs:
        return None, None
    rs.sort()
    lo = rs[max(0, int(0.025 * len(rs)))]
    hi = rs[min(len(rs) - 1, int(0.975 * len(rs)))]
    return lo, hi


def measure_signal(records, name, spec, n_boot, seed):
    """Point-biserial r between `name`'s direction-corrected raw gap and the
    real home-win outcome, over every game where the signal is available on
    BOTH sides and the game had a decisive winner (ties excluded -- no
    direction to correlate a tie against). Returns a measurement report;
    `r` is None if fewer than 3 usable games exist."""
    xs, ys = [], []
    for rec in records:
        if rec["home_win"] is None:
            continue
        gap = _signed_gap(rec["inputs"].get(spec["home_key"]), rec["inputs"].get(spec["away_key"]), spec["favors"])
        if gap is None:
            continue
        xs.append(gap)
        ys.append(1.0 if rec["home_win"] else 0.0)
    r = _pearson_r(xs, ys)
    lo, hi = (None, None) if r is None else _bootstrap_ci(xs, ys, n_boot, seed)
    return {
        "signal": name, "n": len(xs), "r": r, "ci_lo": lo, "ci_hi": hi,
        "stdev_gap": statistics.pstdev(xs) if len(xs) > 1 else None,
    }


def measure_all_signals(records, n_boot, seed):
    return [measure_signal(records, name, spec, n_boot, seed)
            for name, spec in nfl_signals.SIGNAL_SPECS.items()]


def pairwise_signal_correlations(records):
    """Pearson r between every pair of signals' own raw gaps (not against
    the outcome) -- a collinearity check, not a weight input. Reliability-
    proportional weighting (derive_calibration) treats each signal's
    correlation with the outcome as independent evidence; it is NOT,
    if two signals are themselves strongly correlated (scoring_margin is
    partly a summary of exactly what off_epa/def_epa_allowed measure -- both
    are built from points/EPA, just aggregated differently), and this table
    is what lets a reader judge how much of that risk applies rather than
    leaving it invisible. MLB's own methodology caught this same failure
    mode for a candidate signal via an INCREMENTAL likelihood-ratio test
    (season_series added to a model already holding the other three did
    nothing extra); a full multivariate equivalent for NFL (partial
    correlation or logistic regression coefficients controlling for the
    other signals) is a natural follow-up, deliberately not attempted here
    -- a hand-rolled multivariate model with no reference implementation to
    verify it against is a worse rigor trade than clearly flagging the gap.
    """
    names = list(nfl_signals.SIGNAL_SPECS.keys())
    gaps = {}
    for name, spec in nfl_signals.SIGNAL_SPECS.items():
        by_game = {}
        for rec in records:
            gap = _signed_gap(rec["inputs"].get(spec["home_key"]), rec["inputs"].get(spec["away_key"]), spec["favors"])
            if gap is not None:
                by_game[rec["game_id"]] = gap
        gaps[name] = by_game

    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = set(gaps[a]) & set(gaps[b])
            if len(shared) < 3:
                out[(a, b)] = None
                continue
            xs = [gaps[a][gid] for gid in shared]
            ys = [gaps[b][gid] for gid in shared]
            out[(a, b)] = (_pearson_r(xs, ys), len(shared))
    return out


def derive_calibration(measurements, excluded=()):
    """Reliability-proportional weights from measured r, mirroring
    config.yaml's mlb block: "weights ... scaled by the MEASURED reliability
    of the signal family". No hand-set "domain base split" is applied on
    top the way mlb's was -- there is no established prior for these five
    NFL signals to scale FROM without inventing one, so this is pure
    reliability-proportional weighting (equivalent to a uniform base split).

    "Reliability" here is r^2 (variance explained) -- NOT ceiling-corrected
    the way mlb's shrinkage/weight constants are (config.yaml's mlb block:
    "corrected for the ceiling any team-level predictor can reach",
    established against this repo's own multi-month MLB production data).
    Establishing an equivalent NFL ceiling is its own research project, not
    attempted here; flagged, not silently assumed away.

    Two DIFFERENT reasons a signal can end up unweighted, kept distinct in
    the return value and the report -- collapsing them would hide which
    judgment call did what:
      * DROPPED -- its bootstrap 95% CI spans zero (rest_diff): a
        STATISTICAL result, the same treatment season_series got for MLB
        once measured.
      * EXCLUDED -- `excluded` names it (scoring_margin): a MANUAL decision
        made from the pairwise-correlation table (r=0.71 with off_epa),
        because reliability-proportional weighting assumes each signal is
        independent evidence and scoring_margin measurably is not. Its own
        r-vs-outcome measurement is not in question -- it is excluded
        despite having a real, CI-clears-zero correlation, which a reader
        conflating this with DROPPED would misread as "this signal doesn't
        predict anything," the opposite of why it's out.

    Survivors' reliabilities are renormalized to sum to 1 over whichever
    signals remain after BOTH exclusion rules.

    Returns (weights: {signal: float}, dropped: [signal names, CI-based],
    excluded: [signal names, manual], scales: {signal: stdev of its own raw
    gap} -- every SIGNAL_SPECS entry, whether weighted or not, since
    _base_signals looks up every scale key unconditionally regardless of
    which signals end up weighted).
    """
    survivors, dropped = [], []
    for m in measurements:
        if m["signal"] in excluded:
            continue
        if m["r"] is None or m["ci_lo"] is None:
            dropped.append(m["signal"])
            continue
        if m["ci_lo"] <= 0 <= m["ci_hi"]:
            dropped.append(m["signal"])
        else:
            survivors.append(m)

    reliab = {m["signal"]: m["r"] ** 2 for m in survivors}
    total = sum(reliab.values())
    weights = {name: round(v / total, 4) for name, v in reliab.items()} if total > 0 else {}

    scales = {}
    for m in measurements:
        spec = nfl_signals.SIGNAL_SPECS[m["signal"]]
        # A dropped or unmeasurable signal still needs SOME positive scale
        # (score_game's _base_signals divides by it unconditionally); falls
        # back to the placeholder it already shipped with in config.yaml
        # rather than a fabricated number, since that entry is about to be
        # unweighted (and therefore inert) anyway.
        scales[m["signal"]] = round(m["stdev_gap"], 4) if m["stdev_gap"] else _PLACEHOLDER_SCALES[spec["scale_key"]]

    return weights, dropped, list(excluded), scales


# Only used as a fallback for a signal with too little data to measure a
# stdev at all (see derive_calibration) -- the current config.yaml values,
# not a new guess.
_PLACEHOLDER_SCALES = {
    "off_epa_gap": 0.10, "def_epa_gap": 0.10, "turnover_gap": 0.50,
    "margin_gap": 7.0, "rest_gap": 3.0,
}


# --------------------------------------------------------------------------- #
# Grading -- native NFL grading (nflverse already carries final scores), NOT
# signal_report.grade (MLB box-score-shaped; see module docstring).
# --------------------------------------------------------------------------- #

def build_candidate_config(weights, scales, min_threshold, standout_threshold):
    return {
        "betting_signals": {
            "nfl": {
                "min_threshold": min_threshold,
                "standout_threshold": standout_threshold,
                "scales": {spec["scale_key"]: scales[name] for name, spec in nfl_signals.SIGNAL_SPECS.items()},
                "bet_types": {"moneyline": dict(weights)},
            },
        },
    }


def grade_moneyline(rec, scored):
    """(verdict, side) for one record's moneyline pick against its real
    outcome. 'No clear lean' never gets graded (there is no pick to grade --
    counted separately as a coverage stat, not a loss). A tie game PUSHes
    any real pick, mirroring signal_report._side_verdict's winner=None
    handling for MLB."""
    ml = (scored or {}).get("moneyline") or {}
    side = ml.get("side")
    if not side or side == "No clear lean":
        return "NO_LEAN", None
    if rec["home_win"] is None:
        return "PUSH", side
    winner = rec["home"] if rec["home_win"] else rec["away"]
    return ("HIT" if side == winner else "MISS"), side


def run_backtest(records, config):
    """Score + grade every record under `config`. Returns rows: [(rec,
    scored, verdict, side)]."""
    rows = []
    for rec in records:
        scored = nfl_signals.score_game(config, SPORT_KEY, rec["inputs"], availability=rec["availability"])
        verdict, side = grade_moneyline(rec, scored)
        rows.append((rec, scored, verdict, side))
    return rows


def summarize(rows, label):
    """{HIT, MISS, PUSH, NO_LEAN, n, win_rate} over one subset of rows."""
    counts = {"HIT": 0, "MISS": 0, "PUSH": 0, "NO_LEAN": 0}
    for _, _, verdict, _ in rows:
        counts[verdict] += 1
    graded = counts["HIT"] + counts["MISS"]
    return {
        "label": label, "n": len(rows), **counts,
        "win_rate": (counts["HIT"] / graded) if graded else None,
        "graded": graded,
    }


def print_summary(summary):
    wr = "{:.1%}".format(summary["win_rate"]) if summary["win_rate"] is not None else "n/a"
    print("  {:<12} {:>4} games -- {} HIT / {} MISS / {} PUSH / {} no-lean  (win rate {} on {} decided picks)"
          .format(summary["label"], summary["n"], summary["HIT"], summary["MISS"],
                  summary["PUSH"], summary["NO_LEAN"], wr, summary["graded"]))


def score_distribution(rows):
    """Sorted moneyline scores for every record that had at least a
    computable lean (score present even if under whatever threshold config
    used to grade it) -- feeds the threshold-sensitivity table."""
    scores = []
    for _, scored, _, _ in rows:
        ml = (scored or {}).get("moneyline")
        if ml is not None:
            scores.append(ml["score"])
    return sorted(scores)


def threshold_sensitivity(records, weights, scales, candidates):
    """For each candidate threshold: coverage (% of games that clear it) and
    win rate at that threshold, scored fresh at threshold=0 first so the
    same underlying scores are just re-filtered rather than re-computed per
    candidate."""
    # Alignment (the >=2-signals-agree guard) does not depend on the
    # threshold value at all -- only the score>=threshold comparison does
    # (see nfl_signals._finalize) -- so scoring once at threshold=0 already
    # tells us, per game, both the real side (if aligned) and the score it
    # would need to clear. Re-filtering that per candidate below is
    # therefore equivalent to re-scoring at each threshold, without paying
    # for it.
    zero_cfg = build_candidate_config(weights, scales, 0, 0)
    rows0 = run_backtest(records, zero_cfg)
    out = []
    for t in candidates:
        filtered = []
        for rec, scored, _, _ in rows0:
            ml = (scored or {}).get("moneyline") or {}
            if ml.get("score", 0) >= t and ml.get("side") not in (None, "No clear lean"):
                filtered.append((rec, scored, *grade_moneyline(rec, scored)))
            else:
                filtered.append((rec, scored, "NO_LEAN", None))
        s = summarize(filtered, "t>={}".format(t))
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS,
                   help="seasons to backtest (default: {})".format(DEFAULT_SEASONS))
    p.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT, help="bootstrap resamples per signal")
    p.add_argument("--exclude", nargs="*", default=DEFAULT_EXCLUDED,
                   help="signals to manually exclude from weighting despite a real r-vs-outcome "
                        "correlation (default: {} -- see derive_calibration's docstring for why "
                        "this differs from the CI-based auto-drop)".format(sorted(DEFAULT_EXCLUDED)))
    p.add_argument("--min-threshold", type=int, default=None, help="override the auto-picked min_threshold")
    p.add_argument("--standout-threshold", type=int, default=None, help="override the auto-picked standout_threshold")
    p.add_argument("--out", default=OUTPUT_PATH, help="output JSONL path (default: {})".format(OUTPUT_PATH))
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    session = requests.Session()
    t0 = time.time()

    print("nfl_backtest: collecting {} ...".format(args.seasons))
    records = collect_game_records(session, args.seasons)
    n_reg = sum(1 for r in records if not r["postseason"])
    n_post = sum(1 for r in records if r["postseason"])
    print("nfl_backtest: {} games collected ({} regular season, {} postseason) in {:.0f}s\n"
          .format(len(records), n_reg, n_post, time.time() - t0))

    print("nfl_backtest: measuring signal reliability (point-biserial r vs. real home-win, {} bootstrap resamples)..."
          .format(args.n_boot))
    measurements = measure_all_signals(records, args.n_boot, BOOT_SEED)
    print()
    print("{:<18} {:>6} {:>9} {:>22} {:>10}".format("signal", "n", "r", "95% CI", "stdev(gap)"))
    for m in measurements:
        if m["r"] is None:
            print("{:<18} {:>6} {:>9}".format(m["signal"], m["n"], "n/a (too few games)"))
            continue
        print("{:<18} {:>6} {:>9.4f} [{:>+8.4f}, {:>+8.4f}] {:>10.4f}"
              .format(m["signal"], m["n"], m["r"], m["ci_lo"], m["ci_hi"], m["stdev_gap"] or 0.0))

    print()
    print("nfl_backtest: pairwise signal correlations (collinearity check, NOT a weight input -- see docstring):")
    pairs = pairwise_signal_correlations(records)
    for (a, b), result in pairs.items():
        if result is None:
            print("  {:<18} x {:<18} n/a (too few shared games)".format(a, b))
        else:
            r, n = result
            flag = "  <-- collinear" if r is not None and abs(r) >= 0.5 else ""
            print("  {:<18} x {:<18} r={:>+.4f}  n={}{}".format(a, b, r if r is not None else float("nan"), n, flag))

    weights, dropped, excluded, scales = derive_calibration(measurements, excluded=args.exclude)
    print()
    if excluded:
        print("nfl_backtest: EXCLUDED (manual, collinearity -- see pairwise table above): {}".format(", ".join(excluded)))
    if dropped:
        print("nfl_backtest: DROPPED (95% CI spans zero, or unmeasurable): {}".format(", ".join(dropped)))
    print("nfl_backtest: calibrated weights (reliability-proportional, renormalized over survivors):")
    for name, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        print("  {:<18} {:.4f}".format(name, w))
    print("nfl_backtest: measured scales (stdev of each signal's raw home-away gap):")
    for name, spec in nfl_signals.SIGNAL_SPECS.items():
        print("  {:<18} {} = {:.4f}".format(name, spec["scale_key"], scales[name]))

    print()
    print("nfl_backtest: threshold sensitivity (scored at threshold=0, re-filtered per candidate)...")
    sens = threshold_sensitivity(records, weights, scales, [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70])
    for s in sens:
        print_summary(s)

    min_t = args.min_threshold
    standout_t = args.standout_threshold
    if min_t is None or standout_t is None:
        auto = _pick_threshold(sens)
        min_t = min_t if min_t is not None else auto
        standout_t = standout_t if standout_t is not None else auto
    print()
    print("nfl_backtest: using min_threshold={} standout_threshold={} for final grading"
          .format(min_t, standout_t))

    final_cfg = build_candidate_config(weights, scales, min_t, standout_t)
    rows = run_backtest(records, final_cfg)
    reg_rows = [r for r in rows if not r[0]["postseason"]]
    post_rows = [r for r in rows if r[0]["postseason"]]

    print()
    print("nfl_backtest: FINAL RESULTS (outcome-graded -- real final scores, not an estimate)")
    print_summary(summarize(rows, "combined"))
    print_summary(summarize(reg_rows, "regular season"))
    print_summary(summarize(post_rows, "postseason"))

    write_output(rows, args.out)
    print("\nnfl_backtest: wrote {} rows to {} (backtest-only; data/signal_report_history.jsonl untouched)"
          .format(len(rows), args.out))
    print("nfl_backtest: done in {:.0f}s".format(time.time() - t0))
    return 0


def _pick_threshold(sensitivity_rows):
    """A defensible STARTING threshold from the sensitivity table: the
    LARGEST candidate (most selective) that still keeps at least a third of
    games gradeable -- coverage is monotonically non-increasing as the
    threshold rises (a stricter score filter can only drop games, never add
    them), so this is the highest entry in `sensitivity_rows` (ascending by
    construction) before coverage drops under that floor. This is a
    judgment call, explicitly flagged as one in the report -- MLB's own
    threshold took multiple rounds of REAL production measurement to land
    on 17, not one backtest pass; this is meant as a reasonable place to
    START live iteration, not a final answer."""
    candidate = sensitivity_rows[0]
    for s in sensitivity_rows:
        if s["graded"] and (s["graded"] / s["n"]) >= 0.33:
            candidate = s
        else:
            break
    return int(candidate["label"].split(">=")[1])


def write_output(rows, path):
    """Backtest-only JSONL, reusing signal_report.build_pick_rows/
    build_status_row/append_ledger for the OUTPUT FORMAT and file-write
    mechanics (both fully sport-generic since the precursor PR) -- but NOT
    build_pick_rows' `observed` field, which calls signal_report.
    observed_facts(game) internally, itself hardcoded to MLB's linescore/
    boxscore JSON shape (found, not fixed, while building this -- flagged in
    the report; out of scope for a calibration pass to touch signal_report.py
    again). `point` carries the real final score instead, which is what
    observed_facts would have been standing in for."""
    open(path, "w").close()  # truncate -- each run is a complete, self-contained replay, not an accumulating log
    out = []
    run_id = signal_report._now_iso()
    for rec, scored, verdict, side in rows:
        ml = (scored or {}).get("moneyline") or {}
        pick = {
            "gamePk": rec["game_id"], "away_abbr": rec["away"], "home_abbr": rec["home"],
            "start": None, "bet_type": "moneyline", "market": "moneyline",
            "side": side or ml.get("side"), "score": ml.get("score", 0), "flags": ml.get("flags") or [],
            "point": "{}-{}".format(int(rec["away_score"]), int(rec["home_score"])),
        }
        row = {
            "schema_version": signal_report.SCHEMA_VERSION,
            "date": "{}-W{:02d}".format(rec["season"], rec["week"]),
            "status": signal_report.STATUS_RECORDED, "run_id": run_id, "source": SOURCE,
            "sport": SPORT_KEY, "backtest": True,
            "season": rec["season"], "week": rec["week"], "game_type": rec["game_type"],
            "postseason": rec["postseason"],
            "gamePk": pick["gamePk"], "away": pick["away_abbr"], "home": pick["home_abbr"],
            "bet_type": "moneyline", "market": "moneyline", "side": pick["side"],
            "score": pick["score"], "flags": pick["flags"], "point": pick["point"],
            "verdict": verdict, "basis": "outcome" if verdict in ("HIT", "MISS", "PUSH") else None,
        }
        out.append(row)
    signal_report.append_ledger(out, path=path)


if __name__ == "__main__":
    sys.exit(main())
