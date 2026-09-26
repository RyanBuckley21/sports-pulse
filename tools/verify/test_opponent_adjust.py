"""Regression tests for the CFB opponent-adjustment experiment:
fetchers.cfb.build_team_form_adjusted and cfb_opponent_backtest.py.

Run: python3 -m tools.verify.test_opponent_adjust   (from the repo root)

WHY THIS IS PINNED. The backtest's verdict decides whether production's CFB
form changes, and every way it can be wrong is quiet:

  * THE ADJUSTMENT MUST MOVE THE RIGHT WAY. A team that faced weak defenses
    must lose offensive credit, one that faced strong offenses must gain
    defensive credit. App State after two weeks (East Carolina, Charlotte) is
    the case from 2026-09-25; its raw 0.055 PPA allowed must rise.
  * IT MUST STAY POINT-IN-TIME. A week-W game may only see weeks < W, or the
    backtest grades the future.
  * THE HARNESS MUST BE WALK-FORWARD AND PAIRED. Training seasons strictly
    before the test season; variants compared on the same resampled games.
  * THE AUC MUST BE RIGHT, ties included (a no-lean game scores 0).
  * THE CFBD CAP MUST HOLD: the backtest names its own cap and no call goes
    past it.
  * PRODUCTION MUST SCORE ON THE FORM THE WEIGHTS WERE FIT ON.

Sabotage-checked when written (PYTHONDONTWRITEBYTECODE=1), 21 checks: flipping
the sign of the opponent correction fails 1 (App State's defense); letting week
W into a week-W form fails 1; training on the test season fails 1; ignoring
tied ranks in the AUC fails 1; resampling the two variants separately fails 1.
That last one first passed undetected, because a perfectly separating lean
scores AUC 1.0 on every resample; the check now uses noisy leans.

SINCE 2026-09-26 PRODUCTION SCORES ON THE ADJUSTED FORM, and config.yaml's
weights and scales were fit on it, so the suite also runs the real
build_game_entities on the fixture's week-3 slate and pins that the live PPA
values are build_team_form_adjusted's at FORM_SHRINK_GAMES (26 checks).
Sabotage-checked the same way: production back on build_team_form fails 2;
production at shrink 2.0 instead of the constant fails 1; dropping "opp-adj"
from the card label fails 1; cfb_backtest.py defaulting to --form raw fails 1.

The fixture (cfb_form_fixture.json) is REAL: the cfbfastR 2026 schedule for
weeks 1-3 (FBS-vs-FBS regular season) and the CFBD rows for those weeks
exactly as data/boxscores.json cached them at 9ee4edd. No edits. NO NETWORK.
"""

import json
import os
import sys
import tempfile

import cfb_backtest
import cfb_opponent_backtest as cob
from fetchers import cfb

HERE = os.path.dirname(os.path.abspath(__file__))
FX = json.load(open(os.path.join(HERE, "cfb_form_fixture.json")))
INDEX = cfb.fbs_matchup_index(FX["schedule"])
PPA = [r for w in ("1", "2", "3") for r in cfb._ppa_rows_from_cache(FX["ppa"][w])]
STATS = [r for w in ("1", "2", "3") for r in cfb._stats_rows_from_cache(FX["games_teams"][w])]

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + str(detail) + ")") if detail != "" else ""))


def test_the_adjustment():
    raw = cfb.build_team_form(PPA, STATS, INDEX, 4)
    adj = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 4, shrink_games=2.0)
    app, ncsu = "App State", "NC State"
    check("App State's defense, having faced two weak offenses, is rated worse (more allowed)",
          adj[app]["def_ppa_allowed"] > raw[app]["def_ppa_allowed"],
          (raw[app]["def_ppa_allowed"], adj[app]["def_ppa_allowed"]))
    check("NC State's offense, having faced two stronger defenses, is rated better",
          adj[ncsu]["off_ppa"] > raw[ncsu]["off_ppa"], (raw[ncsu]["off_ppa"], adj[ncsu]["off_ppa"]))
    raw_gap = (raw[app]["off_ppa"] - raw[ncsu]["off_ppa"]) - (raw[app]["def_ppa_allowed"] - raw[ncsu]["def_ppa_allowed"])
    adj_gap = (adj[app]["off_ppa"] - adj[ncsu]["off_ppa"]) - (adj[app]["def_ppa_allowed"] - adj[ncsu]["def_ppa_allowed"])
    check("so the App State - NC State PPA gap that made the 85 narrows", 0 < adj_gap < raw_gap, (raw_gap, adj_gap))
    check("games and turnover_diff are untouched",
          all(adj[t]["games"] == raw[t]["games"] and adj[t]["turnover_diff"] == raw[t]["turnover_diff"]
              for t in raw))
    check("same teams as the raw build", set(adj) == set(raw))
    k1 = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 4, shrink_games=1.0)
    k8 = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 4, shrink_games=8.0)
    offs = [r["offense"]["overall"] for r in PPA
            if str(r["gameId"]) in INDEX and r["offense"]["overall"] is not None]
    mu = sum(offs) / len(offs)
    spread = lambda f: sum(abs(v["off_ppa"] - mu) for v in f.values() if v["off_ppa"] is not None)
    check("more shrinkage pulls ratings toward the league mean", spread(k8) < spread(k1),
          (spread(k1), spread(k8)))
    again = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 4, shrink_games=2.0)
    check("deterministic", again == adj)
    try:
        cfb.build_team_form_adjusted(PPA, STATS, INDEX, 4, shrink_games=0)
        refused = False
    except ValueError:
        refused = True
    check("an unpenalised fit is refused (not identified this early)", refused)


def test_it_stays_point_in_time():
    a3 = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 3, shrink_games=2.0)
    only12 = [r for w in ("1", "2") for r in cfb._ppa_rows_from_cache(FX["ppa"][w])]
    b3 = cfb.build_team_form_adjusted(only12, STATS, INDEX, 3, shrink_games=2.0)
    check("a week-3 form never sees week 3's rows", a3 == b3)


def test_the_harness():
    check("walk-forward folds train strictly before they test",
          cob.folds([2025, 2023, 2024]) == [([2023], 2024), ([2023, 2024], 2025)],
          cob.folds([2025, 2023, 2024]))
    check("a single season needs --smoke", cob.folds([2026]) == [])
    check("AUC: perfect separation is 1.0", cob.auc([3, 2, -1, -2], [True, True, False, False]) == 1.0)
    check("AUC: reversed is 0.0", cob.auc([-3, -2, 1, 2], [True, True, False, False]) == 0.0)
    check("AUC: all tied (every game no-lean) is 0.5", cob.auc([0, 0, 0, 0], [True, False, True, False]) == 0.5)
    check("AUC: one class only is None", cob.auc([1, 2], [True, True]) is None)
    # Noisy leans, so an AUC varies from resample to resample: only a truly
    # paired comparison of a variant with itself is 0 on every one. (A
    # perfectly separating lean would score 1.0 on any resample and let an
    # unpaired bug through, which is what the first version of this check did.)
    labels = [i % 3 != 0 for i in range(60)]
    a = [((i * 37) % 11) - 5 + (3 if y else 0) for i, y in enumerate(labels)]
    obs, lo, hi = cob.paired_auc_diff(a, a, labels, 200, 1)
    check("paired: a variant against itself differs by exactly 0 on every resample",
          (obs, lo, hi) == (0.0, 0.0, 0.0), (obs, lo, hi))


class _Session:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return []
        return R()


def test_the_cfbd_cap():
    saved = {k: os.environ.get(k) for k in ("CFB_ALLOW_CFBD", "CFBD_API_KEY")}
    os.environ["CFB_ALLOW_CFBD"], os.environ["CFBD_API_KEY"] = "1", "test-key-not-real"
    try:
        cfb.allow_cfbd_calls(3)
        s = _Session()
        for _ in range(10):
            cfb._cfbd_get(s, "/games/teams", {"year": 2023, "week": 1})
        check("the backtest's named cap holds: exactly 3 calls of 10 go out", s.calls == 3, s.calls)
        check("  and it is reported as the caller's cap, not the monthly budget",
              cfb._cfbd_calls["named"] is True)
    finally:
        cfb.reset_cfbd_budget()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_collection_carries_every_variant():
    # The real collector, fed the fixture through its own on-disk cache, so it
    # makes no request: the schedule and every final week are already there.
    d = tempfile.mkdtemp()
    json.dump(FX["schedule"], open(os.path.join(d, "sched_2026.json"), "w"))
    json.dump({"ppa": {"2026": FX["ppa"]}, "games_teams": {"2026": FX["games_teams"]}},
              open(os.path.join(d, "form_2026.json"), "w"))
    recs = cfb_backtest.collect_season(None, 2026, d, cob.VARIANTS)
    check("the collector returns the fixture's games", len(recs) == len(FX["schedule"]), len(recs))
    check("the raw variant's inputs are exactly production's inputs",
          all(r["variant_inputs"]["raw"] == r["inputs"] for r in recs))
    check("every record carries every variant",
          all(set(r["variant_inputs"]) == set(cob.VARIANTS) for r in recs))


def test_production_scores_on_the_adjusted_form():
    # THE SWITCH (2026-09-26). config.yaml's cfb weights and scales were fit on
    # build_team_form_adjusted at FORM_SHRINK_GAMES; production scoring on raw
    # form under them would run green and read every PPA gap about twice as
    # wide as it was calibrated for. So the real builder is run on a real
    # week-3 slate (2026-09-19) and its values compared with each form. The
    # schedule is stubbed to the fixture's; weeks 1-2 come from the fixture as
    # the committed cache, so no CFBD request is made (CFB_ALLOW_CFBD unset).
    import yaml
    saved = cfb.get_schedule
    cfb.get_schedule = lambda session, season: [dict(r) for r in FX["schedule"]]
    try:
        cache = {"ppa": {"2026": FX["ppa"]}, "games_teams": {"2026": FX["games_teams"]}}
        ents, _, _ = cfb.build_game_entities(yaml.safe_load(open("config.yaml")), "2026-09-19", cache)
    finally:
        cfb.get_schedule = saved
    adj = cfb.build_team_form_adjusted(PPA, STATS, INDEX, 3, shrink_games=cfb.FORM_SHRINK_GAMES)
    raw = cfb.build_team_form(PPA, STATS, INDEX, 3)
    rows = [(e["context"], side) for e in ents.values() for side in ("away", "home")
            if e["context"].get(side + "_off_ppa") is not None]
    check("the week-3 slate builds with PPA form", len(rows) >= 50, len(rows))
    check("every team's live off/def PPA is the ADJUSTED form's, at FORM_SHRINK_GAMES",
          all(c[side + "_off_ppa"] == adj[c[side + "_team"]]["off_ppa"]
              and c[side + "_def_ppa_allowed"] == adj[c[side + "_team"]]["def_ppa_allowed"]
              for c, side in rows))
    check("  and not the raw averages (they differ for most teams)",
          sum(c[side + "_off_ppa"] != raw[c[side + "_team"]]["off_ppa"] for c, side in rows) > len(rows) // 2)
    labels = [s["label"] for e in ents.values() for s in (e.get("signals") or []) if "PPA" in s["label"]]
    check("the card says the PPA numbers are opponent-adjusted",
          labels and all(l.endswith("opp-adj") for l in labels), labels[:2])
    args = cfb_backtest.parse_args([])
    check("cfb_backtest.py fits on production's form by default (the weights' parity)",
          (args.form, args.shrink) == ("adjusted", cfb.FORM_SHRINK_GAMES), (args.form, args.shrink))


def main():
    for fn in (test_the_adjustment, test_it_stays_point_in_time, test_the_harness,
               test_the_cfbd_cap, test_collection_carries_every_variant,
               test_production_scores_on_the_adjusted_form):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("opponent adjust: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
