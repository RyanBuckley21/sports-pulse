"""Does adjusting CFB team form for opponent strength pick winners better?

    python3 cfb_opponent_backtest.py --cache-dir .cache/cfb_backtest     # 2023-2025
    python3 cfb_opponent_backtest.py --seasons 2026 --smoke --cache-dir D  # plumbing check only

WHY. Every CFB signal is a raw season-to-date PPA average against whoever a team
happened to play. On 2026-09-25 that made App State (+440 at NC State) an 85 on
two games each: App State's against East Carolina and Charlotte, NC State's
against Virginia and Vanderbilt. fetchers.cfb.build_team_form_adjusted is the
standard correction (a ridge-regularised offense/defense fit). This script
measures whether it earns a place in production.

THE COMPARISON, fixed before any result was seen:
  * WALK-FORWARD. Seasons [a, b, c] give folds (train a -> test b) and
    (train a+b -> test c). Each variant is calibrated on its training seasons
    only, with cfb_backtest's own procedure (point-biserial r, bootstrap CI,
    r^2 weights, stdev scales, _pick_threshold), then scored on the test
    season it has never seen.
  * SAME GAMES, SAME FETCH. Every variant is built from the same CFBD rows in
    one collection pass (cfb_backtest.collect_season's form_variants), so the
    variants differ only in the form math.
  * THRESHOLD-FREE PRIMARY METRIC. AUC of the signed lean (Signal Score
    toward home, negative toward away, 0 for no lean) against the real home
    win, over decided test games. Variants calibrate to different scales and
    thresholds, so hit rates at their own thresholds cover different games;
    AUC compares them on every game. Hit rate and coverage at each variant's
    training threshold are reported beside it.
  * PAIRED. Test games are resampled together (2,000 times) and each
    variant's AUC minus raw's is computed on the same resample. An unpaired
    read of two hit rates is how an earlier opponent-adjustment experiment
    reported +2.1pp that was -0.2% done properly (config.yaml, NFL block).

DECISION RULE: a variant beats raw only if the pooled test AUC difference's
95% CI is entirely above zero. If several do, the one with the best mean
TRAINING AUC is preferred, never the best test AUC. The early-season subset
(regular-season games in weeks 2-5) is reported separately; a variant that
is significantly worse there is not shipped whatever its pooled number.

WHAT IT DOES NOT DO: change production. The winner, if any, needs its own PR
that switches fetchers/cfb.py to it and re-derives config.yaml's CFB
weights/scales/threshold with cfb_backtest.py under the new form.

OUTCOME (2026-09-26, run 36212369418): adj_k4 won under the rule above, and
that PR followed -- production now scores on build_team_form_adjusted at
fetchers.cfb.FORM_SHRINK_GAMES with weights re-fit on it. So from then on
--production-weights scores every variant, raw included, with weights fit on
the ADJUSTED form: read it as "how much does the live model rely on the
adjustment", not the pre-switch question.

CFBD COST: about 17 calls a season (one bulk /ppa/games plus one /games/teams
per regular week), so about 51 for 2023-2025, capped by --cfbd-budget (default
60). With --cache-dir every final week is kept, so a rerun costs 0. These calls
are outside the live pipeline's monthly counter and come out of the 300 it
reserves (fetchers.cfb.CFBD_MONTHLY_BUDGET).
"""

import argparse
import functools
import random
import sys
import time

import requests

import cfb_backtest
import cfb_signals
from fetchers import cfb

DEFAULT_SEASONS = [2023, 2024, 2025]
SIGNALS = ("off_ppa", "def_ppa_allowed", "turnover_diff")
THRESHOLD_CANDIDATES = [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70]
EARLY_MAX_WEEK = 5
N_BOOT = 2000
SEED = 20260926

VARIANTS = {
    "raw": cfb.build_team_form,
    "adj_k1": functools.partial(cfb.build_team_form_adjusted, shrink_games=1.0),
    "adj_k2": functools.partial(cfb.build_team_form_adjusted, shrink_games=2.0),
    "adj_k4": functools.partial(cfb.build_team_form_adjusted, shrink_games=4.0),
}


def folds(seasons, smoke=False):
    """[(train_seasons, test_season)], train strictly before test. --smoke
    tests a single season on itself: in-sample, plumbing only, never evidence."""
    seasons = sorted(seasons)
    if smoke:
        return [(seasons, seasons[-1])]
    return [(seasons[:i], seasons[i]) for i in range(1, len(seasons))]


def auc(scores, labels):
    """Mann-Whitney AUC with average ranks for ties: the chance a random
    home win carries a higher signed lean than a random home loss. 0.5 is
    no skill. None when either class is empty."""
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_pos = sum(r for r, y in zip(ranks, labels) if y)
    return (rank_pos - pos * (pos + 1) / 2.0) / (pos * neg)


def with_variant(records, name):
    """The records as if `name` were production: inputs from that variant."""
    out = []
    for r in records:
        vin = r["variant_inputs"][name]
        out.append({**r, "inputs": vin, "measure_inputs": vin})
    return out


def _full_scales(scales):
    """cfb_backtest.build_candidate_config wants a scale for every signal in
    cfb_signals.SIGNAL_SPECS, the unweighted fallback tiers included. Those
    carry no weight here, so their scale cannot affect a score; 1.0 fills
    them. Without this the first calibrated variant raised KeyError."""
    return {**{name: 1.0 for name in cfb_signals.SIGNAL_SPECS}, **scales}


def calibrate(train, n_boot):
    """cfb_backtest's own procedure on the training records, restricted to the
    three signals production weights. Returns (weights, scales, threshold)
    or None when nothing survives."""
    meas = [cfb_backtest.measure_signal(train, s, cfb_backtest.MEASURED_SPECS[s], n_boot, SEED)
            for s in SIGNALS]
    weights, _dropped, _excluded, scales = cfb_backtest.derive_calibration(meas)
    if not weights:
        return None
    scales = _full_scales(scales)
    sens = cfb_backtest.threshold_sensitivity(train, weights, scales, THRESHOLD_CANDIDATES)
    return weights, scales, cfb_backtest._pick_threshold(sens)


def signed_leans(records, weights, scales):
    """Signal Score toward home (+), away (-), or 0 for no lean, per record."""
    cfg = cfb_backtest.build_candidate_config(weights, _full_scales(scales), 0, 0)
    out = []
    for r in records:
        ml = (cfb_signals.score_game(cfg, "cfb", r["inputs"]) or {}).get("moneyline") or {}
        side, score = ml.get("side"), ml.get("score") or 0
        out.append(score if side == r["home_abbr"] else (-score if side == r["away_abbr"] else 0))
    return out


def hits_at(leans, records, threshold):
    """(hits, picks) at `threshold` over decided games."""
    hits = picks = 0
    for s, r in zip(leans, records):
        if r["home_win"] is None or s == 0 or abs(s) < threshold:
            continue
        picks += 1
        hits += (s > 0) == r["home_win"]
    return hits, picks


def paired_auc_diff(leans_a, leans_b, labels, n_boot, seed):
    """(observed, lo, hi) for AUC(a) - AUC(b), resampling games together."""
    obs = auc(leans_a, labels) - auc(leans_b, labels)
    rng = random.Random(seed)
    n = len(labels)
    diffs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        ys = [labels[i] for i in idx]
        a, b = auc([leans_a[i] for i in idx], ys), auc([leans_b[i] for i in idx], ys)
        if a is not None and b is not None:
            diffs.append(a - b)
    diffs.sort()
    return obs, diffs[int(0.025 * len(diffs))], diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))]


def production_calibration(config):
    """(weights, scales, threshold) exactly as config.yaml ships them for CFB,
    restricted to SIGNALS: the --production-weights view, which asks whether
    adjusted inputs help the LIVE model without re-fitting it."""
    c = (config.get("betting_signals") or {}).get("cfb") or {}
    w = (c.get("bet_types") or {}).get("moneyline") or {}
    weights = {s: w[s] for s in SIGNALS if s in w}
    scales = {s: c["scales"][cfb_backtest.MEASURED_SPECS[s]["scale_key"]] for s in SIGNALS}
    return weights, scales, c.get("standout_threshold", 40)


def evaluate(records, seasons, n_boot=N_BOOT, smoke=False, fixed=None):
    """Everything the report needs, as data. Pure: no network, no files.
    `fixed`, when given, is a (weights, scales, threshold) used for every
    variant in place of calibrating on the training seasons."""
    decided = [r for r in records if r["home_win"] is not None]
    fold_rows, pooled = [], {v: {"leans": [], "recs": []} for v in VARIANTS}
    train_auc = {v: [] for v in VARIANTS}
    for train_seasons, test_season in folds(seasons, smoke):
        train = [r for r in decided if r["season"] in train_seasons]
        test = [r for r in decided if r["season"] == test_season]
        for v in VARIANTS:
            vtrain, vtest = with_variant(train, v), with_variant(test, v)
            cal = fixed or calibrate(vtrain, n_boot)
            if cal is None:
                fold_rows.append({"fold": (train_seasons, test_season), "variant": v, "calibrated": False})
                continue
            weights, scales, threshold = cal
            tr_leans = signed_leans(vtrain, weights, scales)
            train_auc[v].append(auc(tr_leans, [r["home_win"] for r in vtrain]))
            leans = signed_leans(vtest, weights, scales)
            labels = [r["home_win"] for r in vtest]
            hits, picks = hits_at(leans, vtest, threshold)
            fold_rows.append({"fold": (train_seasons, test_season), "variant": v, "calibrated": True,
                              "weights": weights, "threshold": threshold,
                              "test_auc": auc(leans, labels), "hits": hits, "picks": picks,
                              "n_test": len(vtest)})
            pooled[v]["leans"].extend(leans)
            pooled[v]["recs"].extend(vtest)

    labels = [r["home_win"] for r in pooled["raw"]["recs"]]
    early = [i for i, r in enumerate(pooled["raw"]["recs"])
             if not r["postseason"] and r["cutoff"] <= EARLY_MAX_WEEK]
    comparisons = {}
    for v in VARIANTS:
        if v == "raw" or len(pooled[v]["leans"]) != len(labels) or not labels:
            continue
        a, b = pooled[v]["leans"], pooled["raw"]["leans"]
        c = {"pooled": paired_auc_diff(a, b, labels, n_boot, SEED)}
        if early:
            c["early"] = paired_auc_diff([a[i] for i in early], [b[i] for i in early],
                                         [labels[i] for i in early], n_boot, SEED)
        c["train_auc"] = (sum(train_auc[v]) / len(train_auc[v])) if train_auc[v] else None
        comparisons[v] = c
    winners = [v for v, c in comparisons.items()
               if c["pooled"][1] > 0 and not ("early" in c and c["early"][2] < 0)]
    pick = max(winners, key=lambda v: comparisons[v]["train_auc"] or 0) if winners else None
    return {"folds": fold_rows, "comparisons": comparisons, "winner": pick,
            "fixed": fixed is not None,
            "n_test": len(labels), "n_early": len(early),
            "raw_pooled_auc": auc(pooled["raw"]["leans"], labels) if labels else None,
            "smoke": smoke}


def render(result, seasons, calls_used):
    """The report as Markdown (printed, and written for the Actions summary)."""
    L = ["# CFB opponent-adjustment backtest", ""]
    if result["smoke"]:
        L += ["**SMOKE RUN: in-sample, plumbing check only. Not evidence.**", ""]
    if result.get("fixed"):
        L += ["Every variant scored with **config.yaml's live CFB weights and scales** (not "
              "re-calibrated): does adjusted form help the model as it ships today?", ""]
    L += ["Seasons {}; {} decided test games ({} early-season, weeks 2-{}). CFBD calls this run: {}."
          .format(" ".join(map(str, seasons)), result["n_test"], result["n_early"], EARLY_MAX_WEEK,
                  calls_used), ""]
    L += ["## Per fold", "",
          "| test season | variant | threshold | test AUC | hits / picks at threshold | weights |",
          "| --- | --- | --- | --- | --- | --- |"]
    for f in result["folds"]:
        if not f["calibrated"]:
            L.append("| {} | {} | - | - | - | nothing survived calibration |".format(f["fold"][1], f["variant"]))
            continue
        hr = "{}/{} ({:.1%})".format(f["hits"], f["picks"], f["hits"] / f["picks"]) if f["picks"] else "0/0"
        L.append("| {} | {} | {} | {:.4f} | {} | {} |".format(
            f["fold"][1], f["variant"], f["threshold"], f["test_auc"], hr,
            ", ".join("{} {:.3f}".format(k, w) for k, w in sorted(f["weights"].items()))))
    L += ["", "## Pooled test games, paired against raw (AUC difference, 95% CI)", "",
          "Raw pooled test AUC: {}".format("{:.4f}".format(result["raw_pooled_auc"])
                                           if result["raw_pooled_auc"] is not None else "n/a"), "",
          "| variant | all test games | early season (weeks 2-{}) | mean training AUC |".format(EARLY_MAX_WEEK),
          "| --- | --- | --- | --- |"]
    fmt = lambda t: "{:+.4f} [{:+.4f}, {:+.4f}]".format(*t) if t else "n/a"
    for v, c in result["comparisons"].items():
        L.append("| {} | {} | {} | {} |".format(v, fmt(c["pooled"]), fmt(c.get("early")),
                                                "{:.4f}".format(c["train_auc"]) if c["train_auc"] else "n/a"))
    L += ["", "## Decision (rule fixed in the script's docstring before any result)", "",
          ("**{}** beats raw: its pooled AUC gain's CI is above zero and it is not worse early "
           "in the season. Next: a PR switching production to it and re-deriving config.yaml's CFB "
           "weights with cfb_backtest.py.".format(result["winner"])) if result["winner"] else
          "**No variant beats raw.** Production stays on unadjusted form; the card's "
          "\"Early read\" note stays as the mitigation."]
    return "\n".join(L) + "\n"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--cfbd-budget", type=int, default=60,
                   help="hard cap on CFBD calls for this run (default 60; about 17 a season)")
    p.add_argument("--n-boot", type=int, default=N_BOOT)
    p.add_argument("--smoke", action="store_true",
                   help="test a single season on itself: plumbing check, never evidence")
    p.add_argument("--report", default=None, help="also write the Markdown report here")
    p.add_argument("--production-weights", action="store_true",
                   help="score every variant with config.yaml's live CFB weights instead of "
                        "calibrating each on its training seasons")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.smoke and len(args.seasons) < 2:
        print("cfb_opponent_backtest: need at least two seasons for a walk-forward fold "
              "(or --smoke for a plumbing check)")
        return 2
    t0 = time.time()
    cfb.allow_cfbd_calls(args.cfbd_budget)
    records = cfb_backtest.collect_game_records(requests.Session(), args.seasons,
                                                args.cache_dir, VARIANTS)
    calls = cfb.cfbd_calls_used()
    print("cfb_opponent_backtest: {} games collected, {} CFBD calls (cap {}) in {:.0f}s"
          .format(len(records), calls, args.cfbd_budget, time.time() - t0))
    if not records:
        print("cfb_opponent_backtest: no games collected -- nothing to compare")
        return 1
    fixed = None
    if args.production_weights:
        import yaml
        with open("config.yaml") as f:
            fixed = production_calibration(yaml.safe_load(f))
    report = render(evaluate(records, args.seasons, args.n_boot, args.smoke, fixed), args.seasons, calls)
    print(report)
    if args.report:
        with open(args.report, "w") as f:
            f.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
