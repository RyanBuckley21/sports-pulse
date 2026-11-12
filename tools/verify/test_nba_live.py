"""Regression tests for the NBA picks path: nba_signals (the score), the fit
that config.yaml ships (nba_backtest --fit), and fetchers.nba's games builder.

Run: python3 -m tools.verify.test_nba_live   (from the repo root)

WHY THIS IS PINNED. NBA went from "data layer only" to picks on the site on
2026-11-11, built in a sandbox that cannot reach ESPN. Everything below fails
silently -- a slate still builds, a card still prints a side:

  * THE SITE MUST SCORE WHAT THE BACKTEST MEASURED. config.yaml's numbers are
    fitted to one model; a builder that fed the scorer different inputs (this
    season's margin for a 3-game club, tonight's own result) would ship them
    against another. Held game by game over all 1,230 games of 2025-26.
  * CONFIG MUST BE THE FIT. Every scale, home shift, threshold and the
    bettability cap is re-derived here from the dataset and compared with what
    config.yaml holds, so a hand-edited number cannot pass.
  * NO LOOK-AHEAD. A game on the slate never contributes to its own lean:
    margins come from games played strictly before the slate date.
  * TIERS DO NOT MIX. This season's margin, once both clubs have 5 games;
    last season's only until then. Never both, and a season margin for one
    club only is not used.
  * HOME COURT IS A SHIFT, AND A NEUTRAL SITE GETS NONE.
  * ONLY GAMES THAT COUNT, ON THE EASTERN DATE. Preseason, All-Star and the
    Cup final never become picks; an off day serves the next slate.
  * A PRICE IS DISPLAY, NEVER AN INPUT (invariant 1): the same game scores
    identically with its odds block removed.
  * FAILURE IS LOUD OR ISOLATED. A season that cannot be read raises (so the
    partition freezes, invariant 4); one unreadable game costs only itself.

Fixtures, all REAL and unedited: nba_games_fixture.json (13 events, every
filter trap, three priced pre-game), nba_seasons_2024_2026.json (3,690 decided
games). Three edges are built by editing ONE field of a real event, each
documented at its assertion: a neutral site, a game in progress, and an
unreadable event.

Sabotage-checked when written (2026-11-11, PYTHONDONTWRITEBYTECODE=1, sources
byte-identical afterwards), failures measured, of 62:
  N1  tiers mix: every weighted signal scores          4 fail
  N2  home shift given at a neutral site               2 fail
  N3  best tier is always last season                  6 fail
  N5  look-ahead: the slate's own date counts          3 fail
  N6  a games floor of 1                               2 fail
  N7  non-counted games reach the slate                2 fail
  N8  an off day shows nothing                         1 fail
  N9  cautions dropped from the card                   2 fail
  N10 cold start never fetches last season             1 fail
  N11 one bad game costs the slate                     1 fail
  N12 a game in progress reads Preview                 1 fail
  N14 the price is dropped from the entity             3 fail
  N15 the season walk starts in the slate's month      7 fail
  C1  a scale edited by hand in config.yaml            4 fail
  C2  home shift set to the folk 3 points              4 fail
  C3  threshold lowered by hand                        3 fail
  C4  bettability cap loosened to CFB's                1 fail
TWO CUTS FAILED NOTHING, AND THAT IS A FINDING ABOUT THE CODE, NOT THE SUITE.
Dropping matchup_inputs' "both clubs or neither" rule (N4) and taking the
pick at threshold 0 in build_one_game (N13) change no output: the scorer
already returns no value for a one-sided tier (signal_core.paired), and
score_game already withholds the side under min_threshold. Both guards are
second lines of defence behind a rule enforced elsewhere, and are kept as
such -- they keep the card's displayed margins and its pick consistent with
the lean if either upstream rule is ever relaxed.
N11 and N14 first CRASHED the suite instead of failing checks; fixed.
"""

import ast
import contextlib
import copy
import io
import json
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import nba_backtest  # noqa: E402
import nba_signals  # noqa: E402
import yaml  # noqa: E402
from fetchers import nba  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail)[:300] if detail else ""))


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def quiet(fn, *args, **kwargs):
    """fn's result with its log lines captured; (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


CONFIG = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))
NBA = CONFIG["betting_signals"]["nba"]
DATASET = os.path.join(HERE, "nba_seasons_2024_2026.json")
BY_SEASON = nba_backtest.load(DATASET)
FX = json.load(open(os.path.join(HERE, "nba_games_fixture.json")))
EVENTS = [v for k, v in FX.items() if not k.startswith("_")]
SCALE_S, SCALE_P = NBA["scales"]["season_margin_gap"], NBA["scales"]["prior_margin_gap"]
SHIFT_S, SHIFT_P = NBA["home_field"]["season_margin"], NBA["home_field"]["prior_margin"]
BAR = NBA["standout_threshold"]


def score(**kw):
    return nba_signals.score_game(CONFIG, "nba", nba_signals.build_inputs("AWY", "HOM", **kw))["moneyline"]


# ----------------------------------------------------------------- 1. the score
ok("an unconfigured sport scores nothing (no defaults to fall back on)",
   nba_signals.score_game({}, "nba", nba_signals.build_inputs("A", "H")) == {}
   and nba_signals.score_game(CONFIG, "wnba", nba_signals.build_inputs("A", "H")) == {})
e = score(home_season_margin=6.0, away_season_margin=-2.0)
ok("score is 100 * tanh((home + shift - away) / scale), rounded",
   e["score"] == int(math.floor(100 * math.tanh((6.0 + SHIFT_S + 2.0) / SCALE_S) + 0.5)), e)
ok("  it names the home side and says which tier produced it",
   e["side"] == "HOM" and e["tier"] == "season_margin", e)
e = score(home_season_margin=-2.0, away_season_margin=12.0)
ok("a much better road club is the lean, by name", e["side"] == "AWY" and e["score"] >= BAR, e)
even = score(home_season_margin=3.0, away_season_margin=3.0)
ok("equal clubs at a true home venue lean HOME, by exactly the shift",
   even["score"] == int(math.floor(100 * math.tanh(SHIFT_S / SCALE_S) + 0.5)) and even["score"] > 0, even)
ok("  but under the bar it is not a pick: 'No clear lean', score kept",
   even["side"] == "No clear lean" and 0 < even["score"] < BAR, even)
neutral = score(home_season_margin=3.0, away_season_margin=3.0, neutral_site=True)
ok("the same clubs at a NEUTRAL site score 0: no home court to give", neutral["score"] == 0, neutral)
flip = score(home_season_margin=0.0, away_season_margin=SHIFT_S)
ok("a road club better by exactly the shift is a dead-even game (score 0)", flip["score"] == 0, flip)

both = score(home_season_margin=10.0, away_season_margin=-5.0,
             home_prior_margin=-9.0, away_prior_margin=9.0)
ok("TIERS DO NOT MIX: with this season's margins on file, last season's are ignored",
   both == score(home_season_margin=10.0, away_season_margin=-5.0) and both["tier"] == "season_margin", both)
only_prior = score(home_prior_margin=-9.0, away_prior_margin=9.0)
ok("  with no season margin, last season's decides -- and the entry says so",
   only_prior["side"] == "AWY" and only_prior["tier"] == "prior_margin", only_prior)
ok("  the prior tier uses ITS OWN scale and shift",
   only_prior["score"] == int(math.floor(100 * abs(math.tanh((-9.0 + SHIFT_P - 9.0) / SCALE_P)) + 0.5)),
   only_prior)
nothing = score()
ok("no margin of either kind: no lean, score 0, no tier",
   nothing == {"side": "No clear lean", "score": 0, "flags": [], "tier": None}, nothing)
half = score(home_season_margin=10.0, away_prior_margin=-5.0, home_prior_margin=1.0)
ok("a margin for ONE club only is not a comparison: that tier is skipped",
   half["tier"] == "prior_margin", half)

scored = {"moneyline": score(home_season_margin=10.0, away_season_margin=-5.0)}
ok("list_markets / top_market return the other sports' shape",
   nba_signals.list_markets(scored) == [{"bet_type": "moneyline", "side": "HOM",
                                         "score": scored["moneyline"]["score"], "flags": []}]
   and nba_signals.top_market(scored, BAR)["side"] == "HOM")
ok("  and nothing when there is no lean",
   nba_signals.list_markets({"moneyline": nothing}) == [] and nba_signals.top_market({"moneyline": nothing}, BAR) is None)
SRC = open(os.path.join(_ROOT, "nba_signals.py")).read()
_imported = sorted({(n.module if isinstance(n, ast.ImportFrom) else a.name)
                    for n in ast.walk(ast.parse(SRC)) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names})
ok("PRICE-BLIND: the scorer imports only signal_core, and no input is a price",
   _imported == ["signal_core"]
   and not any("ml" in k or "odds" in k or "spread" in k for k in nba_signals.build_inputs("A", "H")),
   _imported)

# ---------------------------------------------------- 2. config IS the fit
(FIT, CHOSEN, FITCFG), REPORT = quiet(nba_backtest.fit_report, BY_SEASON)
fitted = FITCFG["betting_signals"]["nba"]
ok("config's scales are the fitted ones (sd of each tier's gap on 2023-25)",
   NBA["scales"] == fitted["scales"], (NBA["scales"], fitted["scales"]))
ok("config's home shifts are the fitted ones (OLS intercept / slope)",
   NBA["home_field"] == fitted["home_field"], (NBA["home_field"], fitted["home_field"]))
ok("config's thresholds are the rule's choice on the fit seasons",
   NBA["min_threshold"] == NBA["standout_threshold"] == CHOSEN == 70, (NBA["min_threshold"], CHOSEN))
ok("config weights exactly the signals the scorer defines",
   set(NBA["bet_types"]["moneyline"]) == set(nba_signals.SIGNAL_SPECS) == set(FIT))
ok("the fit used only the first two seasons (2,460 games) and tested the third (1,230)",
   "fitted on seasons 2024, 2025 (2460 games), tested on 2026 (1230 games)" in REPORT, REPORT[:120])
RECORDS = nba_backtest.all_records(BY_SEASON)
TEST = [r for r in RECORDS if r["season"] == 2026]
picks = nba_backtest.graded(TEST, CONFIG)
ok("held-out 2025-26 with the SHIPPED config: 535 picks, 77.9% right",
   len(picks) == 535 and near(sum(h for *_, h in picks) / len(picks), 0.7794, 5e-4),
   (len(picks), sum(h for *_, h in picks) / max(1, len(picks))))
top = [h for _, entry, _, h in picks if entry["score"] >= 90]
ok("the bettability cap is that season's best band's hit rate (scores 90+: 83.9% on 230)",
   len(top) == 230 and near(NBA["max_break_even"], round(sum(top) / len(top), 4), 1e-9),
   (len(top), sum(top) / max(1, len(top)), NBA["max_break_even"]))
home_picks = [h for _, _, home, h in picks if home]
road_picks = [h for _, _, home, h in picks if not home]
ok("home and road leans hit alike at the same bar (78.9% on 365, 75.9% on 170)",
   len(home_picks) == 365 and len(road_picks) == 170
   and abs(sum(home_picks) / 365 - sum(road_picks) / 170) < 0.05)
ok("the floor is one number in both places", nba.SEASON_FLOOR == nba_backtest.SEASON_FLOOR == 5)

# ------------------------------------- 3. the site scores what was measured
# Every game of 2025-26, through the LIVE path's own functions: rows shaped as
# fetchers.nba.parse_game shapes them, season_margins(before the date),
# matchup_inputs, score_game -- against nba_backtest.score_record.
ROWS = [{"id": str(i), "slate_date": g["date"], "home": g["home"], "away": g["away"],
         "home_score": g["home_score"], "away_score": g["away_score"],
         "completed": True, "neutral_site": g["neutral"]}
        for i, g in enumerate(BY_SEASON[2026])]
PRIOR = nba.build_prior_season_margin([dict(g, completed=True) for g in BY_SEASON[2025]])
margins_by_date = {}
mismatch = []
for rec in TEST:
    if rec["date"] not in margins_by_date:
        margins_by_date[rec["date"]] = nba.season_margins(ROWS, rec["date"])
    live = nba_signals.score_game(CONFIG, "nba", nba.matchup_inputs(
        rec["home"], rec["away"], rec["neutral"], margins_by_date[rec["date"]], PRIOR))["moneyline"]
    if live != nba_backtest.score_record(rec, CONFIG):
        mismatch.append((rec["date"], rec["away"], rec["home"], live, nba_backtest.score_record(rec, CONFIG)))
ok("the live path scores all 1,230 games of 2025-26 exactly as the backtest did",
   len(TEST) == 1230 and not mismatch, mismatch[:2])
opening = min(r["slate_date"] for r in ROWS)
ok("NO LOOK-AHEAD: on opening night no club has a margin, though the season is all on file",
   nba.season_margins(ROWS, opening) == {})
second = sorted({r["slate_date"] for r in ROWS})[1]
played_first = {t for r in ROWS if r["slate_date"] == opening for t in (r["home"], r["away"])}
ok("  the next day, exactly the clubs that played opening night have one game",
   {t: v["games"] for t, v in nba.season_margins(ROWS, second).items()} == {t: 1 for t in played_first})
ok("  a club under the floor has no usable season margin; at the floor it does",
   nba._usable({"X": {"margin": 4.0, "games": 4}}, "X") is None
   and nba._usable({"X": {"margin": 4.0, "games": 5}}, "X") == 4.0 and nba._usable({}, "X") is None)

# ------------------------------------------------------------ 4. one game
PRIOR26 = nba.build_prior_season_margin([dict(g, completed=True) for g in BY_SEASON[2026]])
cha = nba.build_one_game(CONFIG, FX["401909897"], {}, PRIOR26)        # PHI @ CHA, 2026-10-31
ok("PHI @ CHA on last season's real margins (CHA +4.83, PHI -0.18): CHA, 76",
   cha["standout"] == {"bet_type": "moneyline", "side": "CHA", "score": 76, "flags": [], "market": "Moneyline"},
   cha["standout"])
ok("  76 is tanh((4.83 + shift - (-0.18)) / scale) by hand",
   76 == int(math.floor(100 * math.tanh((PRIOR26["CHA"]["margin"] + SHIFT_P - PRIOR26["PHI"]["margin"]) / SCALE_P) + 0.5)))
ok("  the card says it is last season's number, then that it knows no injuries",
   cha["caution_notes"] == [nba.COLD_START_CAUTION, nba.INJURY_CAUTION], cha["caution_notes"])
ok("  its one signal row names the tier it used",
   cha["signals"] == [{"label": "Last season margin / game", "value": "CHA +4.8 · PHI -0.2", "tone": "pos"}],
   cha["signals"])
ok("  identity, tip time in Eastern, status, and the price off the same event",
   (cha["gamePk"], cha["sport"], cha["away"]["abbr"], cha["home"]["abbr"], cha["start"], cha["status"])
   == ("401909897", "nba", "PHI", "CHA", "6:00 PM ET", "Preview")
   and ((cha.get("odds") or {}).get("home_ml"), (cha.get("odds") or {}).get("away_ml")) == (100, -120),
   cha.get("odds"))
ok("  team colours come from team_meta's NBA table", cha["home"]["color"] and cha["away"]["color"])
utah = nba.build_one_game(CONFIG, FX["401909896"], {}, PRIOR26)       # POR @ UTAH
ok("POR @ UTAH: a 59 toward POR is under the bar -- no pick, no cautions, score still shown",
   utah["standout"] is None and utah["best_angle"] is None and utah["caution_notes"] is None
   and utah["betting_signals"]["moneyline"] == {"side": "No clear lean", "score": 59, "flags": [], "tier": "prior_margin"},
   utah["betting_signals"])
SEASON_ON = {"CHA": {"margin": 9.0, "games": 6}, "PHI": {"margin": -6.0, "games": 6}}
live = nba.build_one_game(CONFIG, FX["401909897"], SEASON_ON, PRIOR26)
ok("once both clubs have 5 games the lean is on THIS season, and the early-read note is gone",
   live["betting_signals"]["moneyline"]["tier"] == "season_margin"
   and live["caution_notes"] == [nba.INJURY_CAUTION]
   and live["signals"][0]["label"] == "Points margin / game", live["caution_notes"])
ok("  the context records what the lean was built from",
   live["context"]["tier"] == "season_margin" and live["context"]["home_games"] == 6
   and live["context"]["home_season_margin"] == 9.0, live["context"])
unpriced = copy.deepcopy(FX["401909897"])
unpriced["competitions"][0].pop("odds", None)
bare = nba.build_one_game(CONFIG, unpriced, {}, PRIOR26)
ok("PRICE-BLIND: with the odds block removed the game scores identically and carries no price",
   "odds" not in bare and bare["betting_signals"] == cha["betting_signals"] and bare["standout"] == cha["standout"])
# EDGE, one field edited: a neutral site (neutralSite true on PHI @ CHA).
neutral_ev = copy.deepcopy(FX["401909897"])
neutral_ev["competitions"][0]["neutralSite"] = True
n1 = nba.build_one_game(CONFIG, neutral_ev, {}, PRIOR26)
ok("at a neutral site the same clubs score lower: the home shift is withheld",
   n1["betting_signals"]["moneyline"]["score"] < 76 and n1["context"]["neutral_site"] is True,
   n1["betting_signals"])
# EDGE, one field edited: a game in progress (status state 'in').
live_ev = copy.deepcopy(FX["401909897"])
live_ev["competitions"][0]["status"]["type"]["state"] = "in"
ok("a game in progress reads Live", nba.build_one_game(CONFIG, live_ev, {}, PRIOR26)["status"] == "Live")
ok("a finished game reads Final",
   nba.build_one_game(CONFIG, FX["401909881"], {}, PRIOR26)["status"] == "Final")
ok("an event with no usable timestamp has no tip label, not a crash",
   nba._tip_label({"date": "soon"}) is None and nba._tip_label({}) is None)
ok("tip times follow the clocks: 02:00Z on 2025-11-02 is still daylight time (10:00 PM ET, "
   "four hours before the change), and 00:30Z in January is standard time (7:30 PM ET)",
   nba._tip_label({"date": "2025-11-02T02:00Z"}) == "10:00 PM ET"
   and nba._tip_label({"date": "2026-01-10T00:30Z"}) == "7:30 PM ET")

# ------------------------------------------------------- 5. the slate builder
class _Resp:
    def __init__(self, events):
        self._events = events

    def raise_for_status(self):
        pass

    def json(self):
        return {"events": self._events}


class _Session:
    """Serves the fixture's events by ESPN month key (dates=YYYYMM)."""

    def __init__(self, events, fail=None):
        self.events, self.fail, self.calls = events, fail or (), []

    def get(self, url, params=None, timeout=None):
        key = params["dates"]
        self.calls.append(dict(params))
        if key in self.fail:
            raise OSError("ESPN down for " + key)
        return _Resp([e for e in self.events if (e.get("date") or "")[:7].replace("-", "") == key])


def build(date, events=EVENTS, fail=None):
    sess = _Session(events, fail)
    (ents, cache, training), log = quiet(nba.build_game_entities, CONFIG, date, {}, session=sess)
    return ents, log, sess, (cache, training)


ents, log, sess, extra = build("2026-10-31")
want = {k for k, e in FX.items() if not k.startswith("_")
        and nba.is_counted_game(e) and nba.slate_date(e) == "2026-10-31"}
ok("2026-10-31: exactly the counted games of that Eastern date", set(ents) == want and len(want) >= 3,
   (sorted(ents), sorted(want)))
ok("  the builder's other two returns are the empty cache and no training rows", extra == ({}, []))
ok("  one request per month, never a range, limit 500 (invariant 11)",
   all(len(c["dates"]) == 6 and c.get("limit") == 500 for c in sess.calls), sess.calls[:3])
ok("  the season is walked from October", sess.calls[0]["dates"] == "202610", sess.calls[0])
ok("  cold start (no club has 5 games yet): last season is fetched, and said so",
   "202510" in [c["dates"] for c in sess.calls] and "nba cold start" in log, log)
ok("  the three priced pre-game events carry their price",
   {k for k, e in ents.items() if e.get("odds")} >= {"401909896", "401909897", "401909898"})
ok("  the log line counts games, leans and prices", "nba 2026-10-31 -- " in log and " priced" in log, log)

off, log, _, _ = build("2026-10-29")
ok("a day with no games serves the NEXT slate, and says which",
   set(off) == want and "showing the next slate instead (2026-10-31)" in log, (sorted(off), log))
none_ents, log, _, _ = build("2026-07-15")
ok("midsummer: no slate within the lookahead, so nothing (not last season's games)", none_ents == {}, sorted(none_ents))
cup, log, _, _ = build("2025-12-16")
ok("the NBA Cup final's date builds no pick for it (it does not count toward the 82)",
   not any(nba.slate_date(FX[k]) == "2025-12-16" for k in cup), sorted(cup))
star, log, _, _ = build("2026-02-15")
ok("nor does All-Star Sunday", not any(nba.slate_date(FX[k]) == "2026-02-15" for k in star), sorted(star))

try:
    build("2026-10-31", fail=("202610",))
    raised = False
except Exception:  # noqa: BLE001
    raised = True
ok("a month that cannot be read RAISES: the partition freezes, it is not cleared (invariant 4)", raised)
# EDGE, one field edited: an unreadable event (its competitors emptied).
broken = copy.deepcopy(EVENTS)
for ev in broken:
    if ev["id"] == "401909896":
        ev["competitions"][0]["competitors"] = []
try:
    part, log, _, _ = build("2026-10-31", events=broken)
except Exception as exc:  # noqa: BLE001 -- the failure under test (N11 first crashed the suite)
    part = {"raised": repr(exc)}
ok("one unreadable game costs only itself", set(part) == want - {"401909896"}, sorted(part))

NO_SEASON_KEYS = {"away", "best_angle", "betting_signals", "caution_notes", "compare", "context",
                  "est_total", "f5_total", "gamePk", "home", "probables", "pulse", "signal_scores",
                  "signals", "sport", "standout", "start", "start_utc", "status", "venue"}
ok("an entity carries every field the store and the cards read (NFL's set, less win_prob)",
   set(bare) == NO_SEASON_KEYS and set(cha) == NO_SEASON_KEYS | {"odds"}, sorted(set(cha) ^ NO_SEASON_KEYS))

# ------------------------------------------------------------ 6. registered
import generate_insights  # noqa: E402
import generate_stats  # noqa: E402
import signal_report  # noqa: E402
import bet_board  # noqa: E402
ok("NBA has a games builder, a grading adapter and a name",
   generate_insights.GAME_BUILDERS.get("nba") is nba.build_game_entities
   and "nba" in signal_report.SPORT_ADAPTERS and generate_stats.SPORT_LABELS.get("nba") == "NBA"
   and bet_board.SPORT_LABELS.get("nba") == "NBA")
ad = signal_report.SPORT_ADAPTERS["nba"]
ok("  the adapter has every key the grader calls, and defers games not on the graded date",
   set(ad) == set(signal_report.SPORT_ADAPTERS["nfl"]) and ad["store_spans_dates"] is True, sorted(ad))
ok("  its picks are live: nba is in active_game_sports",
   "nba" in (CONFIG.get("active_game_sports") or []))
ok("  and its leaderboards (since 2026-11-11, test_nba_leaderboards): a stats fetcher, in active_sports",
   generate_stats.SPORT_FETCHERS.get("nba", {}).get("fetch") is nba.fetch
   and "nba" in (CONFIG.get("active_sports") or []))
pick = {"away_abbr": "PHI", "home_abbr": "CHA", "side": "CHA", "bet_type": "moneyline"}
ok("  a builder's pick is one the grader can settle (pending until the game is final)",
   ad["grade"](pick, FX["401909897"], False)[1] == "PENDING")

print("nba live: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nba live: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
