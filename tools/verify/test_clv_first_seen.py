"""Regression tests for measuring closing-line value from OUR FIRST CAPTURE
instead of the book's open, and for the report never printing the second as
if it were the first.

Run: python3 -m tools.verify.test_clv_first_seen   (from the repo root)

WHY THIS IS PINNED. On 2026-10-22 the project's first "evidence of edge" --
NFL line movement of +7.90pp toward shipped picks -- turned out to be the
baseline. ESPN's `*_ml_open` is the book's own opening number, which can
predate our first sighting of a game by days; re-anchored on the price when
the pick first existed, NFL fell to -0.50pp [-1.69, +0.76] and every sport
spanned zero. Yet on 2026-10-29 production printed CFB's book-open mean,
+1.87pp on 133 picks, with no caveat at all, because the only caveat was
`n < 100`. Four things are pinned, all silent when wrong:

  1. THE ANCHOR IS WRITTEN ONCE, at the backfill's condition (a scored
     standout AND a moneyline for its side), and never overwritten -- on the
     real PHI @ JAX store history it holds -285 while the line runs to -410
     and closes at -395. A game first stored UNPRICED (cfb 401856713) or with
     NO SCORED STANDOUT (nfl 2026_04_IND_WAS) anchors only when both exist.
     The snapshot survives a run whose fetch has dropped the line.
  2. THE GRADED SIDE READS ITS OWN ANCHOR PRICE from a snapshot of both
     sides, so a standout that flips sides cannot compare one side's anchor
     with the other's close -- the one thing the backfill got wrong.
  3. THE LEDGER ROW CARRIES BOTH BASELINES, each named, as separate fields.
     On PHI @ JAX: +5.8pp from first capture, +32.2pp from the open -- the
     exact pair CLAUDE.md quotes, and the latter matching the real ledger row.
  4. THE REPORT NAMES EVERY BASELINE. First capture is the headline with a
     95% bootstrap interval and a verdict; the book-open line is always
     labelled NOT A MODEL RESULT and gets no verdict, at any n; the two
     pools are never merged.

The fixture is REAL: first_seen_fixture.json holds each game's committed store
entry at named commits of data/insights.games.json, trimmed, unedited. One
edge was never captured -- a run whose fetch dropped the line while the store
still held it -- so that case blanks ONLY the `odds` field of PHI @ JAX's
final real state (documented at the assertion). Nothing reads data/.

Sabotage-checked when written (2026-10-29, PYTHONDONTWRITEBYTECODE=1 and
__pycache__ cleared), counts measured, of 37, sources byte-identical after:
  * S1, snapshot re-taken every run instead of kept: 6 fail -- the anchor
    walks to the close (-285 -> -298 -> -410 -> -395) and PHI @ JAX's
    first-capture CLV collapses to +0.0pp, i.e. the model would have been
    credited with NOTHING ever, the mirror image of the book-open error.
  * S2, no scored-standout requirement: 1 fails -- the one-field edge below.
    Before that case was added this sabotage PASSED SILENTLY, because an
    absent standout already has no side to price; it is kept as a record of
    why the edge exists.
  * S3, anchor price read for the snapshot's side instead of the graded side
    (the backfill's behaviour): 1 fails -- the flipped-side case, -49.6pp.
  * S4, a verdict on the book-open line: 1 fails ("interval clears zero"
    printed beside our picks at n=133).
  * S5, the two baselines pooled: 3 fail -- the headline reports book-open
    movement as first capture.
"""

import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import espn_odds  # noqa: E402
import generate_insights as gi  # noqa: E402
import signal_report as sr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


STATES = json.load(open(os.path.join(HERE, "first_seen_fixture.json")))["states"]


def replay(key, entries=None):
    """Run a game's real committed states through the store writer in order,
    each as one run, exactly as generate_insights would. Returns the stored
    entry after every run."""
    pk = key.split("/")[1]
    store, after = {}, []
    for i, ent in enumerate(entries or [s["entry"] for s in STATES[key]]):
        store = gi._carry_forward_games_store({pk: copy.deepcopy(ent)}, store,
                                              "run-%d" % i, ent.get("slate_date"))
        after.append(store[pk])
    return after


# --------------------------------------------- 1. written once, at the anchor
phi = replay("nfl/2026_05_PHI_JAX")
fs0 = phi[0].get("odds_first_seen") or {}
ok("PHI @ JAX anchors on its first real state (a scored JAX standout, priced)",
   (fs0.get("home_ml"), fs0.get("away_ml")) == (-285, 230), fs0)
ok("  stamped with the run that saw it", fs0.get("captured_at") == "run-0", fs0)
ok("  and the standout side it was taken for", fs0.get("side") == "JAX", fs0)
ok("  NEVER overwritten while the line runs -298 -> -410 -> -395",
   all(s.get("odds_first_seen") == phi[0]["odds_first_seen"] for s in phi),
   [s.get("odds_first_seen", {}).get("home_ml") for s in phi])
ok("  while `odds` itself moves to the close as before (-395)",
   phi[-1]["odds"]["home_ml"] == -395, phi[-1]["odds"].get("home_ml"))

tenn = replay("cfb/401856713")
ok("a game first stored UNPRICED does not anchor (cfb 401856713 @ e4ae0a8)",
   tenn[0].get("odds_first_seen") is None, tenn[0].get("odds_first_seen"))
ok("  and anchors on the run that first prices it (+470 / -650 @ 7c57440)",
   (tenn[1].get("odds_first_seen") or {}).get("captured_at") == "run-1"
   and (tenn[1]["odds_first_seen"]["home_ml"], tenn[1]["odds_first_seen"]["away_ml"])
   == (STATES["cfb/401856713"][1]["entry"]["odds"]["home_ml"],
       STATES["cfb/401856713"][1]["entry"]["odds"]["away_ml"]),
   tenn[1].get("odds_first_seen"))

ind = replay("nfl/2026_04_IND_WAS")
ok("a game first stored with NO SCORED STANDOUT does not anchor, even priced "
   "(IND @ WAS @ 8a1a229, -192 on the board)",
   ind[0].get("odds_first_seen") is None, ind[0].get("odds_first_seen"))
ok("  and anchors when the pick appears (WAS 51 @ 180295b: home WAS +170, away IND -205)",
   ((ind[1].get("odds_first_seen") or {}).get("home_ml"),
    (ind[1].get("odds_first_seen") or {}).get("away_ml")) == (170, -205)
   and ind[1]["odds_first_seen"]["captured_at"] == "run-1", ind[1].get("odds_first_seen"))

# NEVER-OBSERVED EDGE, built by editing ONE field: a standout that names a side
# but carries no score. In 53 sampled store commits (2026-09-15..10-29, 4,145
# entries) a standout was always either absent or scored, so the guard is the
# backfill's condition kept for parity, not a fix for something seen. Without
# this case it was undetectable: an ABSENT standout has no side, so for_side
# already refuses it (sabotage S2 passed silently until this was added).
unscored = copy.deepcopy(STATES["nfl/2026_04_IND_WAS"][1]["entry"])
unscored["standout"]["score"] = 0
ok("a standout with a side but NO SCORE does not anchor, even priced",
   replay("nfl/2026_04_IND_WAS", [unscored])[0].get("odds_first_seen") is None)

# NEVER-CAPTURED EDGE, built by editing ONE field: PHI @ JAX's final real state
# with `odds` blanked, as a fetch looks once ESPN drops the line at kickoff.
dropped = copy.deepcopy(STATES["nfl/2026_05_PHI_JAX"][-1]["entry"])
dropped["odds"] = None
phi_drop = replay("nfl/2026_05_PHI_JAX",
                  [s["entry"] for s in STATES["nfl/2026_05_PHI_JAX"]] + [dropped])
ok("a run whose fetch dropped the line keeps the snapshot",
   phi_drop[-1].get("odds_first_seen") == phi[0]["odds_first_seen"],
   phi_drop[-1].get("odds_first_seen"))
ok("  (and the sticky close, as before)", phi_drop[-1]["odds"]["home_ml"] == -395)


# ------------------------------------- 2. the graded side reads its own anchor
snap, close = phi[-1]["odds_first_seen"], phi[-1]["odds"]
jax = espn_odds.clv_first_seen(snap, close, "JAX", "JAX", "PHI", bet_type="moneyline")
ok("first-capture CLV for JAX: -285 -> -395 is +5.8pp toward",
   jax and jax["delta_display"] == "+5.8pp" and jax["direction"] == "toward", jax)
ok("  named as the first_seen baseline, with when it was anchored",
   jax and jax["baseline"] == espn_odds.BASELINE_FIRST_SEEN and jax["anchored_at"] == "run-0", jax)
phi_side = espn_odds.clv_first_seen(snap, close, "PHI", "JAX", "PHI", bet_type="moneyline")
ok("had the pick flipped to PHI, it is measured from PHI's OWN anchor (+230), "
   "not JAX's (-285)", phi_side and phi_side["open"] == 230 and phi_side["close"] == 310,
   phi_side)
ok("a non-moneyline market is not measured", espn_odds.clv_first_seen(
   snap, close, "JAX", "JAX", "PHI", bet_type="run_line") is None)
ok("no snapshot is unmeasurable, not zero movement",
   espn_odds.clv_first_seen(None, close, "JAX", "JAX", "PHI", bet_type="moneyline") is None)


# ---------------------------------------- 3. the ledger row carries both, named
cfg = sr.load_config(os.path.join(_ROOT, "config.yaml"))
pk = "2026_05_PHI_JAX"
picks = sr.collect_picks({pk: phi[-1]}, cfg, 0, False, sport_key="nfl")
pick = next((p for p in picks if p["bet_type"] == "moneyline"), None)
ok("the grader's pick carries first-capture CLV from the stored snapshot",
   pick and (pick.get("clv_first") or {}).get("delta_display") == "+5.8pp", pick and pick.get("clv_first"))
ok("  and book-open CLV +32.2pp -- the value the real 2026-10-11 ledger row holds",
   pick and (pick.get("clv") or {}).get("delta") == 0.3218, pick and pick.get("clv"))
rows = sr.build_pick_rows("2026-10-11", [(pick, None, "r", "HIT", "outcome")], "fixture", "run-x",
                          sport_key="nfl", sport=sr.SPORT_ADAPTERS["nfl"])
row = rows[0]
ok("the ledger row has a separate clv_first_delta", row.get("clv_first_delta") == jax["delta"], row)
ok("  beside the unchanged clv_delta", row.get("clv_delta") == 0.3218, row.get("clv_delta"))
ok("  each block naming its baseline",
   row["clv"]["baseline"] == "book_open" and row["clv_first"]["baseline"] == "first_seen")
ok("  on schema 4", row.get("schema_version") == 4 == sr.SCHEMA_VERSION, row.get("schema_version"))
old_store_row = copy.deepcopy(phi[-1])
old_store_row.pop("odds_first_seen")
old_pick = next(p for p in sr.collect_picks({pk: old_store_row}, cfg, 0, False, sport_key="nfl")
                if p["bet_type"] == "moneyline")
ok("a store row from before the field yields clv_first None, not a guess",
   old_pick.get("clv_first") is None and old_pick.get("clv") is not None, old_pick.get("clv_first"))


# -------------------------------------------- 4. the report names every baseline
def lines(rows_):
    return sr.clv_record_lines(rows_)


# The 2026-10-29 production shape: lots of book-open history, nothing first-seen.
book_only = [{"verdict": "HIT", "clv_delta": 0.02 + 0.001 * i} for i in range(133)]
out = lines(book_only)
ok("with only book-open history, the headline says first capture is not measured yet",
   out and out[0].startswith("Line movement from OUR FIRST CAPTURE: none measured yet"), out[:1])
ok("  the book-open figure is labelled NOT A MODEL RESULT at n=133",
   any("NOT A MODEL RESULT" in l for l in out), out)
ok("  and carries no verdict, however far its interval sits from zero",
   not any("clears zero" in l for l in out) and any("says nothing about the model" in l for l in out), out)
ok("  and no line has the old unlabelled form", not any(l.startswith("Line movement:") for l in out), out)

mixed = ([{"verdict": "HIT", "clv_delta": 0.08, "clv_first_delta": d} for d in (0.01, -0.02, 0.0, 0.03)]
         + [{"verdict": "MISS", "clv_delta": 0.05}])
out = lines(mixed)
ok("first capture is the HEADLINE when it exists", "OUR FIRST CAPTURE" in out[0] and "none" not in out[0], out)
ok("  its mean is over first-capture deltas only (+0.50pp on 4)", "+0.50pp" in out[1] and "of 4" in out[1], out[1])
ok("  with an interval and a verdict", "[" in out[1] and "spans zero" in out[1], out[1])
ok("  the book-open pool is its own (5 rows, mean +7.40pp), never merged in",
   any("+7.40pp" in l and "of 5" in l for l in out), out)
ok("the interval is deterministic (seed 17)", lines(mixed) == out)
ok("an interval that clears zero says so",
   "interval clears zero" in lines([{"verdict": "HIT", "clv_first_delta": 0.02 + 0.001 * i}
                                    for i in range(20)])[1])
ok("one first-capture pick prints no interval", "n=1, no interval" in
   lines([{"verdict": "HIT", "clv_first_delta": 0.01}])[1])
ok("nothing measurable says NOTHING, rather than printing 0.0pp",
   lines([{"verdict": "HIT", "clv_delta": None, "clv_first_delta": None}]) == [])
ok("an ungraded row is not evidence", lines([{"clv_delta": 0.05, "clv_first_delta": 0.05}]) == [])


print("clv first seen: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "clv first seen: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
