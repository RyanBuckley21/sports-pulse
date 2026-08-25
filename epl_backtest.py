#!/usr/bin/env python3
"""EPL match_result backtest + weight calibration against real completed seasons.

    python3 epl_backtest.py                      # 2019/20-2025/26, full calibration
    python3 epl_backtest.py --seasons 2023 2024 2025
    python3 epl_backtest.py --gate 8 --threshold 60

Mirrors nfl_backtest.py's role (a standalone, read-mostly replay of real
history against the live scoring path, writing its own output file, never
touching the live ledger or config.yaml) and follows its calibration method:
measure each signal's raw predictive validity with a bootstrap CI, derive
reliability-proportional weights, drop what does not clear zero, read
thresholds off a sensitivity table.

THREE DELIBERATE DEPARTURES from that method, each because soccer differs, and
each visible in the report rather than buried:

1. OUTCOME VARIABLE. nfl_backtest correlates against home_win with ties
   excluded. EPL draws are 23.6% of the sample, so exclusion would discard a
   quarter of the evidence and condition every r on "given the match was
   decisive" -- not the population the live model scores. Reliability is
   measured against HOME GOAL DIFFERENCE, continuous and monotone in the 3-way
   result. The home_win version is reported beside it for comparability; they
   agree closely, which is the evidence that this is a better estimator rather
   than a different question.

2. WALK-FORWARD EVALUATION. nfl_backtest derives weights over all seasons and
   then grades on those same seasons -- in-sample twice over. With seven
   seasons available here, --walk-forward (default on) refits on seasons < S
   for each S and grades only on S, so the headline hit rate is out of sample.
   The shipped weights are then fitted on everything, which is what nfl and cfb
   ship too; the walk-forward number is what says whether that fit generalises.

3. COLLINEARITY IS DECIDED BY MEASUREMENT, not by a fixed cutoff. Every
   candidate here is a function of the same match results, so the pairwise
   table is dense (goal_diff is r=0.93 with form_ppm and 0.90 with attack --
   it is close to a linear combination of the other two). --sets prints the
   out-of-sample accuracy of each candidate set with a paired bootstrap
   against the baseline set, so an exclusion can be justified by what it costs
   rather than by a threshold nobody can defend.

Point-in-time discipline: a match on date D in season S sees ONLY matches from
season S played strictly before D. Nothing carries across a season boundary --
promotion and relegation turn over three clubs a year, so last season's form is
a different league.

Nothing here writes to config.yaml. Output is a report (stdout) plus a
backtest-only JSONL (data/epl_backtest_2019_2025.jsonl by default), sport-tagged
"epl", separate from data/signal_report_history.jsonl exactly as
nfl_backtest.py's own output file is.
"""

import argparse
import collections
import datetime
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile

import epl_signals
import signal_core

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
DEFAULT_SEASONS = list(range(2019, 2026))  # 2019/20 .. 2025/26, seven complete seasons
OUTPUT_PATH = "data/epl_backtest_2019_2025.jsonl"
SOURCE = "backtest:espn.eng.1.scoreboard+epl_signals.score_game"
SPORT_KEY = "epl"

N_BOOT_DEFAULT = 2000
BOOT_SEED = 20260825  # fixed, so a re-run reproduces the same CI rather than jittering

# The set this calibration ships. See derive_calibration for why the other four
# candidates are out, and --sets for what excluding them costs.
SHIPPED_SIGNALS = ("attack", "defense", "recent_form", "venue_form")

# Rejected for collinearity rather than for failing to predict -- kept distinct
# from the CI-based drop in the report, because a reader conflating the two
# would read "excluded" as "does not predict anything", the opposite of the
# reason. Each is paired with the retained signal it duplicates.
EXCLUDED_FOR_COLLINEARITY = {
    "goal_diff": "attack (r=0.90) and defense (r=0.83) -- close to their linear combination",
    "form_ppm": "attack (r=0.84) and venue_form (r=0.84)",
    "recent_gd": "recent_form (r=0.89)",
}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

def _fetch_json(url, cache_dir=None, key=None):
    """One GET, via curl.

    curl rather than requests deliberately: this endpoint gzips unconditionally
    and some sandboxed environments' proxies reject requests' handshake to this
    host with a 403 while curl succeeds. The fetcher in fetchers/epl.py uses
    requests and is unaffected in production -- this is a backtest-only tool and
    portability of the replay matters more here than matching that transport.
    """
    path = os.path.join(cache_dir, key + ".json") if (cache_dir and key) else None
    if path and os.path.exists(path):
        return json.load(open(path))
    out = path or tempfile.mkstemp(suffix=".json")[1]
    subprocess.run(["curl", "-sS", "--compressed", url, "-o", out], check=True)
    return json.load(open(out))


def collect_matches(seasons, cache_dir=None):
    """Every completed Premier League match in `seasons`, oldest first.

    One scoreboard call per season. The window is Aug 1 -> Jul 31: ESPN 400s on
    any range wider than about a year, and that window returns exactly 380 per
    season including 2019/20, which COVID pushed out to 26 July 2020.

    Season membership comes from each event's own `season.slug`, not from the
    date window, so a fixture that spills outside its window cannot be filed
    under the wrong season.
    """
    matches = []
    for year in seasons:
        url = "%s?dates=%d0801-%d0731&limit=1000" % (SCOREBOARD, year, year + 1)
        payload = _fetch_json(url, cache_dir, "sb_%d" % year)
        for e in payload.get("events") or []:
            season = e.get("season") or {}
            if "english-premier-league" not in (season.get("slug") or ""):
                continue
            comp = (e.get("competitions") or [{}])[0]
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            sides = {}
            for c in comp.get("competitors") or []:
                team = c.get("team") or {}
                try:
                    score = int(c.get("score"))
                except (TypeError, ValueError):
                    score = None
                sides[c.get("homeAway")] = {"id": team.get("id"),
                                            "abbr": team.get("abbreviation"), "score": score}
            if set(sides) != {"home", "away"}:
                continue
            if any(s["score"] is None for s in sides.values()):
                continue
            matches.append({"id": e["id"], "date": (e.get("date") or "")[:10],
                            "season": season.get("year"), "slug": season.get("slug"),
                            "home": sides["home"], "away": sides["away"]})
    matches.sort(key=lambda m: (m["date"], m["id"]))
    return matches


def _points(gf, ga):
    return 3 if gf > ga else (1 if gf == ga else 0)


def build_records(matches, window=5):
    """Attach point-in-time form to every match. See the module docstring for
    the discipline; the short version is that a match sees only earlier matches
    from its own season."""
    hist = collections.defaultdict(list)
    records = []
    for m in matches:
        season = m["season"]
        inputs = {"home_abbr": m["home"]["abbr"], "away_abbr": m["away"]["abbr"]}
        for side in ("home", "away"):
            past = hist[(season, m[side]["id"])]
            n = len(past)
            inputs[side + "_played"] = n
            if not n:
                for k in ("gf_pm", "ga_pm", "ppm", "gd_pm", "recent_ppm",
                          "recent_gd_pm", "venue_ppm", "rest"):
                    inputs["%s_%s" % (side, k)] = None
                continue
            recent = past[-window:]
            venue = [p for p in past if p["venue"] == side]
            inputs[side + "_gf_pm"] = sum(p["gf"] for p in past) / n
            inputs[side + "_ga_pm"] = sum(p["ga"] for p in past) / n
            inputs[side + "_ppm"] = sum(p["pts"] for p in past) / n
            inputs[side + "_gd_pm"] = sum(p["gf"] - p["ga"] for p in past) / n
            inputs[side + "_recent_ppm"] = sum(p["pts"] for p in recent) / len(recent)
            inputs[side + "_recent_gd_pm"] = sum(p["gf"] - p["ga"] for p in recent) / len(recent)
            # Venue-specific form: the home side's record AT HOME against the
            # away side's record AWAY. The only signal carrying venue
            # information, which matters in a league where the home side wins
            # 43.4% and the away side 32.9% -- see epl_signals' note on the
            # missing intercept.
            inputs[side + "_venue_ppm"] = (sum(p["pts"] for p in venue) / len(venue)) if venue else None
            inputs[side + "_rest"] = float((_d(m["date"]) - _d(past[-1]["date"])).days)
        hs, as_ = m["home"]["score"], m["away"]["score"]
        records.append({
            "id": m["id"], "date": m["date"], "season": m["season"],
            "home_abbr": m["home"]["abbr"], "away_abbr": m["away"]["abbr"],
            "home_score": hs, "away_score": as_, "home_gd": hs - as_,
            "home_win": None if hs == as_ else (hs > as_),
            "result": "H" if hs > as_ else ("A" if as_ > hs else "D"),
            "inputs": inputs,
        })
        for side, gf, ga in (("home", hs, as_), ("away", as_, hs)):
            hist[(season, m[side]["id"])].append(
                {"gf": gf, "ga": ga, "pts": _points(gf, ga), "venue": side, "date": m["date"]})
    return records


def _d(s):
    return datetime.date.fromisoformat(s)


# --------------------------------------------------------------------------- #
# Reliability
# --------------------------------------------------------------------------- #

def signed_gap(inputs, spec):
    """Direction-corrected raw gap: positive always means "favors home",
    matching signal_core.paired's sign convention but WITHOUT the tanh squash
    or the config scale -- reliability must not be conflated with the (also
    being calibrated here) scale choice, and a tanh already saturated near +/-1
    would compress away exactly the variation a correlation needs to read."""
    h, a = inputs.get(spec["home_key"]), inputs.get(spec["away_key"])
    if h is None or a is None:
        return None
    d = h - a
    return d if spec["favors"] == "higher" else -d


def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def bootstrap_ci(xs, ys, n_boot, seed):
    """95% bootstrap CI on r via resampling with replacement. Stdlib only, no
    numpy/scipy dependency -- same constraint nfl_backtest works under.

    n_boot=0 skips it and returns (None, None). The walk-forward refits weights
    once per held-out season and needs only r^2 and the stdev; computing a CI
    inside that loop would multiply the whole run by n_boot for a number
    nothing there reads."""
    if not n_boot:
        return None, None
    rng = random.Random(seed)
    n = len(xs)
    rs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = pearson_r([xs[i] for i in idx], [ys[i] for i in idx])
        if r is not None:
            rs.append(r)
    if not rs:
        return None, None
    rs.sort()
    return rs[max(0, int(0.025 * len(rs)))], rs[min(len(rs) - 1, int(0.975 * len(rs)))]


def measure_signal(records, name, spec, outcome, n_boot, seed):
    """r between `name`'s direction-corrected raw gap and the outcome.

    outcome='gd'  -- home goal difference, every match. Weights come from this.
    outcome='win' -- point-biserial vs home_win, draws excluded. Comparability
                     with the NFL/CFB numbers only.
    """
    xs, ys = [], []
    for rec in records:
        if outcome == "win":
            if rec["home_win"] is None:
                continue
            y = 1.0 if rec["home_win"] else 0.0
        else:
            y = float(rec["home_gd"])
        gap = signed_gap(rec["inputs"], spec)
        if gap is None:
            continue
        xs.append(gap)
        ys.append(y)
    r = pearson_r(xs, ys)
    lo, hi = (None, None) if r is None else bootstrap_ci(xs, ys, n_boot, seed)
    return {"signal": name, "n": len(xs), "r": r, "ci_lo": lo, "ci_hi": hi,
            "stdev_gap": statistics.pstdev(xs) if len(xs) > 1 else None}


def measure_all(records, outcome, n_boot, seed):
    return [measure_signal(records, name, spec, outcome, n_boot, seed)
            for name, spec in epl_signals.SIGNAL_SPECS.items()]


def pairwise_correlations(records):
    """|r| between every pair of candidate signal gaps. Dense by construction
    here -- every candidate is a function of the same match results -- which is
    why the exclusion decision needs this table and not just each signal's own
    correlation with the outcome."""
    names = list(epl_signals.SIGNAL_SPECS)
    gaps = {n: [signed_gap(r["inputs"], epl_signals.SIGNAL_SPECS[n]) for r in records]
            for n in names}
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pairs = [(x, y) for x, y in zip(gaps[a], gaps[b]) if x is not None and y is not None]
            if len(pairs) < 3:
                continue
            out[(a, b)] = pearson_r([x for x, _ in pairs], [y for _, y in pairs])
    return out


def derive_calibration(measurements, shipped=SHIPPED_SIGNALS, require_ci=True):
    """Reliability-proportional weights from measured r, nfl_backtest's method.

    Reliability is r^2 (variance explained), renormalized over the survivors.
    NOT ceiling-corrected the way MLB's constants are -- establishing an EPL
    ceiling is its own research project, flagged rather than assumed away, the
    same position NFL and CFB are in.

    THREE distinct reasons a signal ends up unweighted, kept separate because
    collapsing them would hide which judgment call did what:
      * DROPPED -- its bootstrap 95% CI spans zero (rest). A statistical
        result, the same treatment NFL's rest_diff and MLB's season_series got.
      * EXCLUDED -- collinear with a retained signal (goal_diff, form_ppm,
        recent_gd). A measured decision from the pairwise table: reliability-
        proportional weighting assumes each signal is independent evidence, and
        these measurably are not. Their own correlations with the outcome are
        strong and not in question -- they are out DESPITE predicting well.
      * NOT SHIPPED -- not in `shipped` for any other reason. Empty today.

    Returns (weights, dropped, excluded, scales). `scales` covers EVERY spec,
    weighted or not, because _base_signals looks up every scale key
    unconditionally.
    """
    by_name = {m["signal"]: m for m in measurements}
    survivors, dropped, excluded = [], [], []
    for name in epl_signals.SIGNAL_SPECS:
        m = by_name.get(name) or {}
        if name in EXCLUDED_FOR_COLLINEARITY:
            excluded.append(name)
            continue
        if m.get("r") is None:
            dropped.append(name)
        elif require_ci and m.get("ci_lo") is None:
            dropped.append(name)
        elif require_ci and m["ci_lo"] <= 0 <= m["ci_hi"]:
            dropped.append(name)
        elif name in shipped:
            survivors.append(m)
    reliab = {m["signal"]: m["r"] ** 2 for m in survivors}
    total = sum(reliab.values())
    weights = {n: round(v / total, 4) for n, v in reliab.items()} if total > 0 else {}

    scales = {}
    for name, spec in epl_signals.SIGNAL_SPECS.items():
        m = by_name.get(name) or {}
        # An unmeasurable signal still needs SOME positive scale (_base_signals
        # divides by it unconditionally). 1.0 is inert for an unweighted signal
        # and is not a fabricated measurement.
        scales[spec["scale_key"]] = round(m["stdev_gap"], 4) if m.get("stdev_gap") else 1.0
    return weights, dropped, excluded, scales


# --------------------------------------------------------------------------- #
# Scoring + grading
# --------------------------------------------------------------------------- #

def candidate_config(weights, scales, min_threshold, standout_threshold):
    return {"betting_signals": {SPORT_KEY: {
        "min_threshold": min_threshold, "standout_threshold": standout_threshold,
        "scales": scales, "bet_types": {"double_chance": weights}}}}


def grade(rec, scored):
    """Grade a double_chance pick against the real final score.

    A DRAW WINS. That is what the market is: "<team> or Draw", so a draw is a
    result the pick contains rather than the result that beats it. `hit` grades
    the market as scored. `outright_hit` grades the same lean read as a 1X2
    pick, where a draw loses -- reported alongside because it is the number the
    other sports' moneyline hit rates are comparable to, and because the gap
    between the two is the clearest statement of what including draws buys.
    """
    entry = (scored or {}).get("double_chance") or {}
    side = entry.get("side")
    if not side or side == "No clear lean":
        return None
    # "ARS or Draw" -> the leaning side is the leading token.
    picked_home = side.split(" ")[0] == rec["home_abbr"]
    outright = (rec["result"] == "H") if picked_home else (rec["result"] == "A")
    return {"side": side, "score": entry.get("score", 0), "picked_home": picked_home,
            "result": rec["result"], "hit": outright or rec["result"] == "D",
            "outright_hit": outright}


def fit_weights(records, gate, names, n_boot, seed):
    """Weights + scales from `records` only -- the unit the walk-forward refits.

    n_boot is 0 from the walk-forward: the CI-based drop does not apply there
    because `names` already states which signals the set contains, so the only
    outputs read are r^2 and the stdev."""
    usable = [r for r in records
              if (r["inputs"].get("home_played") or 0) >= gate
              and (r["inputs"].get("away_played") or 0) >= gate]
    ms = measure_all(usable, "gd", n_boot, seed)
    return derive_calibration(ms, shipped=names, require_ci=bool(n_boot))


def walk_forward(records, gate, names, seed, threshold=0):
    """Fit on seasons < S, grade on S, never refit on S. The first season is
    fit-only -- there is nothing earlier to train it on.

    Scored at threshold 0 and filtered afterwards, which is EXACTLY equivalent
    to rescoring per threshold: signal_core.finalize uses the threshold only in
    `score >= threshold and aligned` when assigning a side, and the alignment
    rule does not depend on it. So one pass produces every row of the
    sensitivity table instead of one pass per threshold."""
    seasons = sorted({r["season"] for r in records})
    picks = []
    for season in seasons[1:]:
        train = [r for r in records if r["season"] < season]
        test = [r for r in records if r["season"] == season]
        weights, _, _, scales = fit_weights(train, gate, names, 0, seed)
        if not weights:
            continue
        cfg = candidate_config(weights, scales, 0, 0)
        for rec in test:
            scored = epl_signals.score_game(cfg, SPORT_KEY, rec["inputs"])
            g = grade(rec, scored)
            if g and g["score"] >= threshold:
                picks.append(dict(g, season=season, id=rec["id"], date=rec["date"]))
    return picks


def summarize(picks):
    if not picks:
        return None
    n = len(picks)
    return {"n": n,
            "hit": sum(1 for p in picks if p["hit"]) / n,
            "outright": sum(1 for p in picks if p["outright_hit"]) / n,
            "pct_home": sum(1 for p in picks if p["picked_home"]) / n}


def baselines(records, gate):
    usable = [r for r in records
              if (r["inputs"].get("home_played") or 0) >= gate
              and (r["inputs"].get("away_played") or 0) >= gate]
    n = len(usable) or 1
    return {"n": len(usable),
            "home": sum(1 for r in usable if r["result"] == "H") / n,
            "home_or_draw": sum(1 for r in usable if r["result"] in ("H", "D")) / n}


def compare_sets(a_picks, b_picks, n_boot, seed):
    """How set B differs from set A, out of sample.

    NOT a paired bootstrap on the shared matches, and the reason is a measured
    property of these candidates rather than a preference: on every match both
    sets score above the bar, they pick the SAME SIDE -- 714 of 714 for the two
    leading sets. Adding recent_form and venue_form shifts scores, and so shifts
    WHICH matches clear the threshold, but essentially never flips the sign of
    the lean. A paired test on the intersection is therefore structurally zero
    and would report "no difference" for a reason that has nothing to do with
    whether the extra signals help.

    What actually differs is the pick LIST, so that is what is compared: each
    set's accuracy over its own picks, with an unpaired bootstrap on the
    difference. Returns (agreement, mean_delta_pp, lo, hi) with agreement the
    share of shared matches picked the same way -- reported because a reader
    should see that the sets agree on direction before reading the delta.
    """
    a = {p["id"]: p for p in a_picks}
    b = {p["id"]: p for p in b_picks}
    both = sorted(set(a) & set(b))
    agreement = (sum(1 for i in both if a[i]["side"] == b[i]["side"]) / len(both)) if both else None

    av = [1 if p["hit"] else 0 for p in a_picks]
    bv = [1 if p["hit"] else 0 for p in b_picks]
    if len(av) < 30 or len(bv) < 30:
        return agreement, None, None, None
    rng = random.Random(seed)
    ds = []
    for _ in range(n_boot):
        ra = [av[rng.randrange(len(av))] for _ in range(len(av))]
        rb = [bv[rng.randrange(len(bv))] for _ in range(len(bv))]
        ds.append(sum(rb) / len(rb) - sum(ra) / len(ra))
    ds.sort()
    return (agreement, 100 * statistics.mean(ds), 100 * ds[int(0.025 * len(ds))],
            100 * ds[min(len(ds) - 1, int(0.975 * len(ds)))])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

CANDIDATE_SETS = {
    "attack+defense": ("attack", "defense"),
    "attack+defense+recent": ("attack", "defense", "recent_form"),
    "attack+defense+venue": ("attack", "defense", "venue_form"),
    "SHIPPED (a+d+recent+venue)": SHIPPED_SIGNALS,
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS)
    p.add_argument("--gate", type=int, default=epl_signals.MIN_MATCHES,
                   help="minimum prior matches this season per side (default: %(default)s)")
    p.add_argument("--threshold", type=int, default=None,
                   help="override the auto-picked threshold")
    p.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT)
    p.add_argument("--seed", type=int, default=BOOT_SEED)
    p.add_argument("--sets", action="store_true",
                   help="compare candidate signal sets out of sample")
    p.add_argument("--cache-dir", default=None,
                   help="reuse downloaded scoreboards from this directory")
    p.add_argument("--output", default=OUTPUT_PATH)
    p.add_argument("--no-write", action="store_true")
    return p.parse_args(argv)


def pick_threshold(rows):
    """The most selective candidate that still keeps at least a third of the
    scoreable slate -- nfl_backtest._pick_threshold's rule, and a judgment call
    flagged as one there too. A starting point for live iteration, not a final
    answer: MLB's threshold took several rounds of real production measurement
    to settle on 17, and this has had none."""
    candidate = rows[0]
    for r in rows:
        if r["coverage"] >= 0.33:
            candidate = r
        else:
            break
    return candidate["threshold"]


def main(argv=None):
    args = parse_args(argv)
    print("Collecting completed matches for seasons %s ..." % ", ".join(map(str, args.seasons)))
    matches = collect_matches(args.seasons, args.cache_dir)
    records = build_records(matches)
    per_season = collections.Counter(r["season"] for r in records)
    print("  %d completed matches: %s\n" % (
        len(records), ", ".join("%d/%02d n=%d" % (s, (s + 1) % 100, per_season[s])
                                for s in sorted(per_season))))

    res = collections.Counter(r["result"] for r in records)
    n = len(records) or 1
    print("OUTCOME BASE RATES -- the reason this backtest is not nfl_backtest")
    print("  home win %5.1f%%   draw %5.1f%%   away win %5.1f%%" %
          (100 * res["H"] / n, 100 * res["D"] / n, 100 * res["A"] / n))
    print("  A draw is %.1f%% of matches and loses a match_result pick. NFL excludes" %
          (100 * res["D"] / n))
    print("  its 0.2%% ties from the reliability pass; that is not available here.\n")

    usable = [r for r in records
              if (r["inputs"].get("home_played") or 0) >= args.gate
              and (r["inputs"].get("away_played") or 0) >= args.gate]
    print("SIGNAL RELIABILITY (gate >= %d prior matches per side, n=%d)" % (args.gate, len(usable)))
    gd = {m["signal"]: m for m in measure_all(usable, "gd", args.n_boot, args.seed)}
    win = {m["signal"]: m for m in measure_all(usable, "win", args.n_boot, args.seed)}
    print("  %-13s %7s %-22s %8s %-22s %9s" %
          ("signal", "r(gd)", "95% CI", "r(win)", "95% CI", "stdev gap"))
    for name in epl_signals.SIGNAL_SPECS:
        g, w = gd[name], win[name]
        def cell(m):
            if m["r"] is None:
                return "    n/a", "%-22s" % ""
            flag = " *" if m["ci_lo"] <= 0 <= m["ci_hi"] else ""
            return "%+7.3f" % m["r"], "%-22s" % ("[%+.3f, %+.3f]%s" % (m["ci_lo"], m["ci_hi"], flag))
        rg, cg = cell(g)
        rw, cw = cell(w)
        print("  %-13s %s %s %s %s %9.4f" % (name, rg, cg, rw, cw, g["stdev_gap"] or 0))
    print("  * = 95% CI spans zero\n")

    pw = pairwise_correlations(usable)
    dense = sorted(((abs(v), a, b, v) for (a, b), v in pw.items()), reverse=True)
    print("PAIRWISE |r| >= 0.80 -- why the outcome column alone cannot pick the set")
    for _, a, b, v in dense:
        if abs(v) >= 0.80:
            print("  %-13s %-13s r=%+.3f" % (a, b, v))
    print()

    weights, dropped, excluded, scales = derive_calibration(list(gd.values()))
    print("CALIBRATION (fitted on every season above)")
    for name in SHIPPED_SIGNALS:
        m = gd[name]
        print("  %-13s r=%+.3f  r^2=%.4f  ->  weight %.4f" %
              (name, m["r"], m["r"] ** 2, weights.get(name, 0.0)))
    for name in dropped:
        m = gd[name]
        print("  %-13s DROPPED -- 95%% CI [%+.3f, %+.3f] spans zero" %
              (name, m["ci_lo"], m["ci_hi"]))
    for name in excluded:
        print("  %-13s EXCLUDED -- collinear with %s" % (name, EXCLUDED_FOR_COLLINEARITY[name]))
    print("  scales: %s\n" % ", ".join("%s=%.4f" % (k, v) for k, v in sorted(scales.items())))

    print("THRESHOLD SENSITIVITY (out of sample, walk-forward by season)")
    all_picks = walk_forward(records, args.gate, SHIPPED_SIGNALS, args.seed)
    scoreable = len(all_picks) or 1
    rows = []
    for t in (0, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75):
        s = summarize([p for p in all_picks if p["score"] >= t])
        if not s:
            continue
        rows.append({"threshold": t, "coverage": s["n"] / scoreable, **s})
    print("  %-10s %7s %9s %8s %10s" % ("threshold", "picks", "%scoreable", "DoubleCh", "1X2 (ref)"))
    for r in rows:
        print("  %-10d %7d %8.1f%% %7.1f%% %9.1f%%" %
              (r["threshold"], r["n"], 100 * r["coverage"], 100 * r["hit"], 100 * r["outright"]))
    threshold = args.threshold if args.threshold is not None else pick_threshold(rows)
    print("  -> threshold %d\n" % threshold)

    if args.sets:
        print("CANDIDATE SETS (out of sample, threshold %d)" % threshold)
        base = None
        print("  %-28s %7s %8s %10s %7s  %s" %
              ("set", "picks", "DoubleCh", "1X2 (ref)", "agree", "vs attack+defense"))
        for label, names in CANDIDATE_SETS.items():
            picks = walk_forward(records, args.gate, names, args.seed, threshold)
            s = summarize(picks)
            if base is None:
                base = picks
                agree_s, delta = "  --   ", ""
            else:
                agreement, mean, lo, hi = compare_sets(base, picks, args.n_boot, args.seed)
                agree_s = "%6.1f%%" % (100 * agreement) if agreement is not None else "   n/a"
                delta = ("%+5.2f pp  CI [%+.2f, %+.2f]%s" %
                         (mean, lo, hi, "" if lo <= 0 <= hi else "  *")) if mean is not None else ""
            print("  %-28s %7d %7.1f%% %9.1f%% %s  %s" %
                  (label, s["n"], 100 * s["hit"], 100 * s["outright"], agree_s, delta))
        print("  agree = share of shared matches picked the same side as attack+defense")
        print("  * = bootstrap CI on the accuracy difference excludes zero\n")

    print("THREE-WAY OUTCOME SPLIT by score band (out of sample)")
    print("  The draw as a first-class outcome. `side` is the leaning side winning")
    print("  outright, `other` the opposite side; a double_chance pick wins on")
    print("  side+draw. These are observed frequencies, not a fitted distribution --")
    print("  config.yaml's epl.outcome_split ships the all-season version.")
    print("  %-14s %7s %8s %8s %9s" % ("score band", "n", "side", "draw", "other"))
    for lo, hi in ((0, 25), (25, 50), (50, 70), (70, 101)):
        band = [p for p in all_picks if lo <= p["score"] < hi]
        if not band:
            continue
        bn = len(band)
        side = sum(1 for p in band if p["outright_hit"]) / bn
        draw = sum(1 for p in band if p["result"] == "D") / bn
        print("  %-14s %7d %7.3f %8.3f %9.3f" %
              ("%d-%d" % (lo, hi - 1), bn, round(side, 3), round(draw, 3),
               round(1 - side - draw, 3)))
    print()

    picks = [p for p in all_picks if p["score"] >= threshold]
    s = summarize(picks)
    b = baselines(records, args.gate)
    print("GRADED PERFORMANCE at threshold %d (out of sample)" % threshold)
    print("  %-11s %7s %8s %10s" % ("season", "picks", "DoubleCh", "1X2 (ref)"))
    for season in sorted({p["season"] for p in picks}):
        sp = [p for p in picks if p["season"] == season]
        print("  %-11s %7d %7.1f%% %9.1f%%" %
              ("%d/%02d" % (season, (season + 1) % 100), len(sp),
               100 * sum(1 for p in sp if p["hit"]) / len(sp),
               100 * sum(1 for p in sp if p["outright_hit"]) / len(sp)))
    print("  %-11s %7d %7.1f%% %9.1f%%" % ("ALL", s["n"], 100 * s["hit"], 100 * s["outright"]))
    print("  picked home on %.1f%% of them; home wins %.1f%% of the gated slate" %
          (100 * s["pct_home"], 100 * b["home"]))
    print("  naive baselines: always HOME-or-DRAW %.1f%%   (1X2 ref: always HOME %.1f%%)" %
          (100 * b["home_or_draw"], 100 * b["home"]))
    print("\n  THIS IS A HIT RATE, NOT A PROFITABILITY CLAIM. No odds, line or price")
    print("  data enters this repo anywhere, so nothing here says these picks beat")
    print("  the vig. Draws are priced; a 1X2 hit rate cannot see that.\n")

    if not args.no_write:
        write_output(picks, weights, scales, threshold, args.output)
        print("wrote %s (%d rows)" % (args.output, len(picks)))
    return 0


def write_output(picks, weights, scales, threshold, path):
    """Backtest-only JSONL, sport-tagged and kept separate from the live ledger
    exactly as nfl_backtest's own output file is."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(path, "w") as fh:
        fh.write(json.dumps({"kind": "calibration", "sport": SPORT_KEY, "source": SOURCE,
                             "generated_at": stamp, "weights": weights, "scales": scales,
                             "threshold": threshold}) + "\n")
        for p in picks:
            fh.write(json.dumps({"kind": "pick", "sport": SPORT_KEY, "source": SOURCE,
                                 "generated_at": stamp, **p}) + "\n")


if __name__ == "__main__":
    sys.exit(main())
