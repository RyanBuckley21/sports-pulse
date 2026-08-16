"""CFB moneyline backtest + weight calibration, mirroring nfl_backtest.py's
methodology on a 3-season window (2023-2025).

A SEPARATE SCRIPT rather than a --sport flag on nfl_backtest.py, matching the
fetcher/signals split already in the repo. The two share methodology, not
code: the data-collection halves have almost nothing in common (nflverse
season files with a rest column and an injury report vs. a per-season
schedule CSV plus two CFBD endpoints with a week fan-out), and the
postseason handling below is CFB-specific in a way that would be pure
special-casing inside a shared collector. What IS shared is the statistical
procedure, deliberately kept identical so the two calibrations are
comparable: direction-corrected raw gaps, point-biserial r against the real
home-win outcome, 95% bootstrap CI, reliability-proportional (r^2) weights
renormalized over survivors, and a threshold picked off a sensitivity table.

NO REIMPLEMENTED SIGNAL LOGIC. Every record's inputs come from the SAME
fetchers/cfb.py functions the live path calls (build_team_form,
build_scoring_margins, fbs_matchup_index), and grading scores them through
cfb_signals.score_game verbatim. If production and this script ever disagree
about what a signal means, that is a bug in one of them, not a modelling
choice made here.

POSTSEASON WEEK RENUMBERING -- the one place this script must NOT copy
production. CFBD numbers postseason weeks from 1 again (in 2025, all 46
FBS-vs-FBS bowl/playoff games carry week=1), so passing a bowl's raw week
into build_team_form's `upto_week` filters out the ENTIRE regular season and
leaves every form value None. Production currently does exactly that -- see
the PR discussion; it means live CFB postseason games would all score "No
clear lean". Here, a postseason game's effective cutoff is instead
max(regular week) + 1, i.e. "everything that had actually been played before
the bowl", which is what point-in-time discipline actually means for a game
played after the regular season ends. Regular-season games are unchanged:
strictly weeks < W.

FOUR signals are MEASURED, but only three are in cfb_signals.SIGNAL_SPECS.
scoring_margin is computed by fetchers/cfb.build_scoring_margins and carried
in each game's `context`, deliberately unweighted, because NFL's calibration
found it collinear with offensive EPA. This script measures it fresh rather
than assuming that carries over -- college football's blowout margins are far
wider than the NFL's, so the collinearity question is genuinely open. See
MEASURED_SPECS.
"""

import argparse
import json
import os
import random
import statistics
import time

import requests
import yaml

import cfb_signals
from fetchers import cfb

SPORT_KEY = "cfb"
DEFAULT_SEASONS = [2023, 2024, 2025]
N_BOOT_DEFAULT = 2000
BOOT_SEED = 20260816
OUTPUT_PATH = "data/cfb_backtest_2023_2025.jsonl"

# Manual exclusions, applied AFTER measurement. Empty by default and that is
# the point of this pass: NFL's equivalent defaults to {"scoring_margin"},
# but presupposing that here would answer the exact question this backtest
# was asked to open. Populate it via --exclude once the pairwise table gives
# a reason.
DEFAULT_EXCLUDED = set()

# The measured candidate set: everything cfb_signals scores, PLUS
# scoring_margin, which fetchers/cfb.py computes but no config weight
# references. Reliability measurement only needs a home key, an away key and
# a direction -- it reads raw gaps, never the tanh/scale path -- so a
# candidate outside SIGNAL_SPECS can be measured without being scoreable.
# Grading with it weighted is a different matter; see _register_candidate.
MEASURED_SPECS = dict(cfb_signals.SIGNAL_SPECS)
MEASURED_SPECS["scoring_margin"] = {
    "home_key": "home_scoring_margin", "away_key": "away_scoring_margin",
    "scale_key": "margin_gap", "favors": "higher",
}

# Fallback scales for a signal too sparse to measure a stdev for -- the
# current config.yaml placeholders, not a new guess. margin_gap has no
# config entry yet (scoring_margin is unweighted); NFL's calibrated value is
# used as the stand-in and is only ever reached if scoring_margin turns out
# unmeasurable, in which case it is inert anyway.
_PLACEHOLDER_SCALES = {
    "off_ppa_gap": 0.10, "def_ppa_gap": 0.10, "turnover_gap": 0.50,
    "margin_gap": 10.1113,
}


# --------------------------------------------------------------------------- #
# Collection -- point-in-time, FBS-vs-FBS, via the production fetcher.
# --------------------------------------------------------------------------- #

def _cache_get(cache_dir, key, produce):
    """Optional on-disk memo for a raw API response. A full 3-season
    collection is ~60 CFBD calls; caching makes re-running the statistics
    (different --exclude, different thresholds) free instead of re-fetching
    the same immutable history. Completed seasons never change, so this is
    safe to keep across runs -- but it is opt-in via --cache-dir rather than
    on by default, so an unattended run can never serve stale data silently."""
    if not cache_dir:
        return produce()
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    value = produce()
    with open(path, "w") as f:
        json.dump(value, f)
    return value


def collect_season(session, season, cache_dir=None):
    """Every gradeable FBS-vs-FBS game in one season, with point-in-time
    inputs built by fetchers/cfb.py.

    Fetch cost per season: 1 schedule CSV + 2 bulk /ppa/games (regular +
    postseason) + one /games/teams per regular week + one for postseason.
    Unlike production -- which only ever needs weeks before the slate it is
    building -- a backtest needs the whole season, so the week fan-out runs
    to completion here."""
    schedule = _cache_get(cache_dir, "sched_{}".format(season),
                          lambda: cfb.get_schedule(session, season))
    if not schedule:
        print("cfb_backtest: {} -- no schedule published, skipped".format(season))
        return []

    fbs_index = cfb.fbs_matchup_index(schedule)          # regular season, FBS-vs-FBS only
    reg_weeks = sorted({e["week"] for e in fbs_index.values()})
    max_reg_week = cfb.max_regular_week(fbs_index)

    ppa = _cache_get(cache_dir, "ppa_{}".format(season),
                     lambda: cfb.get_ppa_games(session, season))
    team_stats = _cache_get(cache_dir, "gt_{}".format(season),
                            lambda: cfb.get_team_game_stats(session, season, reg_weeks))

    print("cfb_backtest: {} -- {} schedule rows, {} FBS-vs-FBS regular games, "
          "weeks {}-{}, {} ppa rows, {} team-stat games"
          .format(season, len(schedule), len(fbs_index), min(reg_weeks or [0]),
                  max_reg_week, len(ppa), len(team_stats)))

    # Form is memoized per cutoff: every game in week W shares one form
    # table, and all postseason games share the end-of-regular-season one.
    form_cache, margin_cache = {}, {}

    def form_at(cutoff):
        if cutoff not in form_cache:
            form_cache[cutoff] = cfb.build_team_form(ppa, team_stats, fbs_index, cutoff)
            margin_cache[cutoff] = cfb.build_scoring_margins(schedule, fbs_index, cutoff)
        return form_cache[cutoff], margin_cache[cutoff]

    records, skipped = [], 0
    for g in schedule:
        if g.get("home_division") != "fbs" or g.get("away_division") != "fbs":
            continue
        season_type = g.get("season_type")
        if season_type not in ("regular", "postseason"):
            continue
        h_pts, a_pts = cfb._num(g.get("home_points")), cfb._num(g.get("away_points"))
        if h_pts is None or a_pts is None:
            continue                      # unplayed
        try:
            raw_week = int(g["week"])
        except (TypeError, ValueError, KeyError):
            skipped += 1
            continue

        postseason = season_type == "postseason"
        # THE postseason correction -- see the module docstring. A bowl's
        # raw week=1 would otherwise wipe out the entire regular season.
        # Imported from fetchers/cfb.py rather than reimplemented, so this
        # script and production cannot disagree about it.
        cutoff = cfb.form_cutoff(g, max_reg_week)

        form, margins = form_at(cutoff)
        away, home = g["away_team"], g["home_team"]
        away_form, home_form = form.get(away, {}), form.get(home, {})
        # Same abbreviation labels production uses, resolved through the same
        # helper. The label never touches the scoring math -- it only decides
        # what string `side` comes back as -- but if this script graded
        # school names while production emits abbreviations, the two would
        # disagree about what a pick LOOKS like, which is exactly the kind of
        # production/backtest drift the postseason cutoff bug came from.
        away_abbr = cfb._team_ref(away)["abbr"]
        home_abbr = cfb._team_ref(home)["abbr"]

        inputs = cfb_signals.build_inputs(
            away_abbr=away_abbr, home_abbr=home_abbr,
            away_off_ppa=away_form.get("off_ppa"), home_off_ppa=home_form.get("off_ppa"),
            away_def_ppa_allowed=away_form.get("def_ppa_allowed"),
            home_def_ppa_allowed=home_form.get("def_ppa_allowed"),
            away_turnover_diff=away_form.get("turnover_diff"),
            home_turnover_diff=home_form.get("turnover_diff"),
        )
        # Measured-only candidate, kept OUT of `inputs` so score_game's
        # contract is exactly what production passes it.
        extra = {"away_scoring_margin": margins.get(away), "home_scoring_margin": margins.get(home)}

        records.append({
            "game_id": str(g["game_id"]), "season": season,
            "week": raw_week, "cutoff": cutoff, "postseason": postseason,
            "away": away, "home": home,
            "away_abbr": away_abbr, "home_abbr": home_abbr,
            "away_score": a_pts, "home_score": h_pts,
            "home_win": None if h_pts == a_pts else (h_pts > a_pts),
            "inputs": inputs,
            "measure_inputs": {**inputs, **extra},
        })
    if skipped:
        print("cfb_backtest: {} -- {} rows skipped (unparseable week)".format(season, skipped))
    return records


def collect_game_records(session, seasons, cache_dir=None):
    records = []
    for season in seasons:
        records.extend(collect_season(session, season, cache_dir))
    return records


# --------------------------------------------------------------------------- #
# Reliability measurement -- identical procedure to nfl_backtest.py.
# --------------------------------------------------------------------------- #

def _signed_gap(home_val, away_val, favors):
    """Direction-corrected raw gap: positive always means "favors home",
    matching cfb_signals._paired's sign convention but WITHOUT the tanh
    squash or the config scale. Reliability must not be conflated with the
    scale choice being calibrated in the same pass -- a tanh already
    saturated near +/-1 would compress away exactly the variation a
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
    """95% bootstrap CI on Pearson r via resampling with replacement --
    stdlib only, no scipy/numpy, same as nfl_backtest."""
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
    return rs[max(0, int(0.025 * len(rs)))], rs[min(len(rs) - 1, int(0.975 * len(rs)))]


def measure_signal(records, name, spec, n_boot, seed):
    """Point-biserial r between `name`'s direction-corrected raw gap and the
    real home-win outcome, over games where the signal is available on BOTH
    sides and the game had a decisive winner."""
    xs, ys = [], []
    for rec in records:
        if rec["home_win"] is None:
            continue
        gap = _signed_gap(rec["measure_inputs"].get(spec["home_key"]),
                          rec["measure_inputs"].get(spec["away_key"]), spec["favors"])
        if gap is None:
            continue
        xs.append(gap)
        ys.append(1.0 if rec["home_win"] else 0.0)
    r = _pearson_r(xs, ys)
    lo, hi = (None, None) if r is None else _bootstrap_ci(xs, ys, n_boot, seed)
    return {"signal": name, "n": len(xs), "r": r, "ci_lo": lo, "ci_hi": hi,
            "stdev_gap": statistics.pstdev(xs) if len(xs) > 1 else None}


def measure_all_signals(records, n_boot, seed):
    return [measure_signal(records, name, spec, n_boot, seed)
            for name, spec in MEASURED_SPECS.items()]


def pairwise_signal_correlations(records):
    """Pearson r between every pair of candidates' own raw gaps (not against
    the outcome) -- a collinearity check, not a weight input.

    Reliability-proportional weighting treats each signal's correlation with
    the outcome as independent evidence, which it is not if two signals are
    themselves strongly correlated. This is the table that decides whether
    NFL's scoring_margin exclusion carries over to CFB or not, and it is
    deliberately measured rather than assumed. Same acknowledged limitation
    as NFL's: this is pairwise, not a multivariate/partial-correlation test,
    because a hand-rolled multivariate model with nothing to verify it
    against is a worse rigor trade than flagging the gap."""
    names = list(MEASURED_SPECS)
    gaps = {}
    for name, spec in MEASURED_SPECS.items():
        per_game = {}
        for rec in records:
            gap = _signed_gap(rec["measure_inputs"].get(spec["home_key"]),
                              rec["measure_inputs"].get(spec["away_key"]), spec["favors"])
            if gap is not None:
                per_game[rec["game_id"]] = gap
        gaps[name] = per_game

    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = sorted(set(gaps[a]) & set(gaps[b]))
            if len(shared) < 3:
                out[(a, b)] = None
                continue
            r = _pearson_r([gaps[a][g] for g in shared], [gaps[b][g] for g in shared])
            out[(a, b)] = (r, len(shared))
    return out


def derive_calibration(measurements, excluded=()):
    """Reliability-proportional weights from measured r, same discipline as
    nfl_backtest.derive_calibration.

    "Reliability" is r^2 (variance explained), NOT ceiling-corrected the way
    config.yaml's mlb constants are -- establishing a CFB ceiling is its own
    research project, flagged rather than silently assumed away.

    Two distinct reasons a signal ends up unweighted, kept separate because
    collapsing them hides which judgment call did what:
      * DROPPED -- its bootstrap 95% CI spans zero. A statistical result.
      * EXCLUDED -- named in `excluded`. A manual decision from the pairwise
        table, applied despite a real correlation with the outcome.

    Returns (weights, dropped, excluded, scales)."""
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
        spec = MEASURED_SPECS[m["signal"]]
        scales[m["signal"]] = (round(m["stdev_gap"], 4) if m["stdev_gap"]
                               else _PLACEHOLDER_SCALES[spec["scale_key"]])
    return weights, dropped, list(excluded), scales


# --------------------------------------------------------------------------- #
# Grading -- native, from the schedule's own final scores.
# --------------------------------------------------------------------------- #

def _register_candidate(name):
    """Make a MEASURED-only candidate scoreable by registering it into
    cfb_signals.SIGNAL_SPECS for this process.

    Only reached when a candidate outside the shipped SIGNAL_SPECS survives
    measurement and earns a weight -- today that can only be scoring_margin.
    This is a RUNTIME registration for grading, and it is exactly the change
    that would have to be made PERMANENTLY in cfb_signals.py (plus a
    margin_gap entry under config.betting_signals.cfb.scales) for production
    to score the same way. The report says so explicitly rather than letting
    a backtest silently grade with a signal production cannot compute."""
    if name not in cfb_signals.SIGNAL_SPECS:
        cfb_signals.SIGNAL_SPECS[name] = MEASURED_SPECS[name]


def build_candidate_config(weights, scales, min_threshold, standout_threshold):
    for name in weights:
        _register_candidate(name)
    return {
        "betting_signals": {
            SPORT_KEY: {
                "min_threshold": min_threshold,
                "standout_threshold": standout_threshold,
                "scales": {spec["scale_key"]: scales[name]
                           for name, spec in cfb_signals.SIGNAL_SPECS.items()},
                "bet_types": {"moneyline": dict(weights)},
            },
        },
    }


def grade_moneyline(rec, scored):
    """(verdict, side) for one record's moneyline pick against its real
    outcome. 'No clear lean' is never graded -- there is no pick -- and is
    counted as coverage, not a loss. A tie PUSHes any real pick."""
    ml = (scored or {}).get("moneyline") or {}
    side = ml.get("side")
    if not side or side == "No clear lean":
        return "NO_LEAN", None
    if rec["home_win"] is None:
        return "PUSH", side
    # Compare against the ABBREVIATION, matching what score_game now returns
    # as `side` (see collect_season).
    winner = rec["home_abbr"] if rec["home_win"] else rec["away_abbr"]
    return ("HIT" if side == winner else "MISS"), side


def run_backtest(records, config):
    rows = []
    for rec in records:
        scored = cfb_signals.score_game(config, SPORT_KEY, rec["inputs"])
        verdict, side = grade_moneyline(rec, scored)
        rows.append((rec, scored, verdict, side))
    return rows


def summarize(rows, label):
    counts = {"HIT": 0, "MISS": 0, "PUSH": 0, "NO_LEAN": 0}
    for _, _, verdict, _ in rows:
        counts[verdict] += 1
    graded = counts["HIT"] + counts["MISS"]
    return {"label": label, "n": len(rows), **counts,
            "win_rate": (counts["HIT"] / graded) if graded else None, "graded": graded}


def print_summary(s):
    wr = "{:.1%}".format(s["win_rate"]) if s["win_rate"] is not None else "n/a"
    print("  {:<16} {:>5} games -- {} HIT / {} MISS / {} PUSH / {} no-lean  "
          "(win rate {} on {} decided picks)"
          .format(s["label"], s["n"], s["HIT"], s["MISS"], s["PUSH"], s["NO_LEAN"], wr, s["graded"]))


def home_baseline(records):
    """Naive always-pick-home win rate over decided games -- the bar any
    moneyline model has to clear to be worth anything. Reported alongside
    the model, same as NFL's."""
    decided = [r for r in records if r["home_win"] is not None]
    if not decided:
        return None, 0
    wins = sum(1 for r in decided if r["home_win"])
    return wins / len(decided), len(decided)


def threshold_sensitivity(records, weights, scales, candidates):
    """Coverage and win rate per candidate threshold. Scored once at
    threshold=0 and re-filtered: the >=2-agree alignment guard does not
    depend on the threshold value, only the score>=threshold comparison
    does, so re-filtering is equivalent to re-scoring without paying for
    it."""
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
        out.append(summarize(filtered, "t>={}".format(t)))
    return out


def _pick_threshold(sensitivity_rows):
    """A defensible STARTING threshold: the most selective candidate that
    still keeps at least a third of games gradeable. Coverage is
    monotonically non-increasing as the threshold rises, so this is the
    highest entry before coverage drops under that floor. A judgment call,
    flagged as one -- MLB's threshold took multiple rounds of real
    production measurement, not one backtest pass."""
    candidate = sensitivity_rows[0]
    for s in sensitivity_rows:
        if s["graded"] and (s["graded"] / s["n"]) >= 0.33:
            candidate = s
        else:
            break
    return int(candidate["label"].split(">=")[1])


def write_output(rows, path):
    """Backtest-only JSONL -- one row per game, carrying the real final
    score and the graded verdict. data/signal_report_history.jsonl (the
    production ledger) is never touched by this script."""
    with open(path, "w") as f:
        for rec, scored, verdict, side in rows:
            ml = (scored or {}).get("moneyline") or {}
            f.write(json.dumps({
                "sport": SPORT_KEY, "game_id": rec["game_id"], "season": rec["season"],
                "week": rec["week"], "cutoff_week": rec["cutoff"], "postseason": rec["postseason"],
                "away": rec["away"], "home": rec["home"],
                "away_score": rec["away_score"], "home_score": rec["home_score"],
                "pick": side, "score": ml.get("score"), "verdict": verdict,
            }, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS)
    p.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT)
    p.add_argument("--exclude", nargs="*", default=sorted(DEFAULT_EXCLUDED),
                   help="candidates to exclude from weighting despite a real correlation "
                        "(collinearity); default none -- see pairwise table")
    p.add_argument("--min-threshold", type=int, default=None)
    p.add_argument("--standout-threshold", type=int, default=None)
    p.add_argument("--cache-dir", default=None,
                   help="optional dir memoizing raw API responses across runs")
    p.add_argument("--out", default=OUTPUT_PATH)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    session = requests.Session()
    t0 = time.time()

    print("cfb_backtest: collecting {} ...".format(args.seasons))
    records = collect_game_records(session, args.seasons, args.cache_dir)
    n_reg = sum(1 for r in records if not r["postseason"])
    n_post = sum(1 for r in records if r["postseason"])
    print("cfb_backtest: {} games collected ({} regular season, {} postseason) in {:.0f}s\n"
          .format(len(records), n_reg, n_post, time.time() - t0))

    base_rate, base_n = home_baseline(records)
    if base_rate is not None:
        print("cfb_backtest: naive always-pick-home baseline: {:.1%} ({} decided games)\n"
              .format(base_rate, base_n))

    print("cfb_backtest: measuring reliability (point-biserial r vs. real home-win, "
          "{} bootstrap resamples)...".format(args.n_boot))
    measurements = measure_all_signals(records, args.n_boot, BOOT_SEED)
    print()
    print("{:<18} {:>6} {:>9} {:>22} {:>11}".format("signal", "n", "r", "95% CI", "stdev(gap)"))
    for m in measurements:
        if m["r"] is None:
            print("{:<18} {:>6} {:>9}".format(m["signal"], m["n"], "n/a"))
            continue
        print("{:<18} {:>6} {:>9.4f} [{:>+8.4f}, {:>+8.4f}] {:>11.4f}"
              .format(m["signal"], m["n"], m["r"], m["ci_lo"], m["ci_hi"], m["stdev_gap"] or 0.0))

    print()
    print("cfb_backtest: pairwise candidate correlations (collinearity check, NOT a weight input):")
    for (a, b), result in pairwise_signal_correlations(records).items():
        if result is None:
            print("  {:<18} x {:<18} n/a".format(a, b))
        else:
            r, n = result
            flag = "  <-- collinear" if abs(r) >= 0.5 else ""
            print("  {:<18} x {:<18} r={:>+.4f}  n={}{}".format(a, b, r, n, flag))

    weights, dropped, excluded, scales = derive_calibration(measurements, excluded=args.exclude)
    print()
    if excluded:
        print("cfb_backtest: EXCLUDED (manual, collinearity): {}".format(", ".join(excluded)))
    if dropped:
        print("cfb_backtest: DROPPED (95% CI spans zero, or unmeasurable): {}".format(", ".join(dropped)))
    print("cfb_backtest: calibrated weights (reliability-proportional, renormalized over survivors):")
    for name, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        print("  {:<18} {:.4f}".format(name, w))
    print("cfb_backtest: measured scales (stdev of each candidate's raw home-away gap):")
    for name in weights:
        print("  {:<18} {} = {:.4f}".format(name, MEASURED_SPECS[name]["scale_key"], scales[name]))

    print()
    print("cfb_backtest: threshold sensitivity (scored at threshold=0, re-filtered per candidate)...")
    sens = threshold_sensitivity(records, weights, scales, [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70])
    for s in sens:
        print_summary(s)

    min_t, standout_t = args.min_threshold, args.standout_threshold
    if min_t is None or standout_t is None:
        auto = _pick_threshold(sens)
        min_t = min_t if min_t is not None else auto
        standout_t = standout_t if standout_t is not None else auto
    print()
    print("cfb_backtest: using min_threshold={} standout_threshold={} for final grading"
          .format(min_t, standout_t))

    final_cfg = build_candidate_config(weights, scales, min_t, standout_t)
    rows = run_backtest(records, final_cfg)
    reg_rows = [r for r in rows if not r[0]["postseason"]]
    post_rows = [r for r in rows if r[0]["postseason"]]

    print()
    print("cfb_backtest: FINAL RESULTS (outcome-graded -- real final scores, not an estimate)")
    print_summary(summarize(rows, "combined"))
    print_summary(summarize(reg_rows, "regular season"))
    print_summary(summarize(post_rows, "postseason"))
    for season in args.seasons:
        srows = [r for r in rows if r[0]["season"] == season]
        if srows:
            print_summary(summarize(srows, str(season)))

    write_output(rows, args.out)
    print("\ncfb_backtest: wrote {} rows to {} (backtest-only; "
          "data/signal_report_history.jsonl untouched)".format(len(rows), args.out))
    print("cfb_backtest: done in {:.0f}s".format(time.time() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
