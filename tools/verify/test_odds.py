"""Price capture: the conversions, the parsing, and the one rule that makes the
whole thing work -- a stored price is never cleared.

WHY THIS FILE EXISTS. Every hit rate in this repo answers "was the pick right".
None of them answer "did it make money", and for a model with no price input
those come apart hard: 48% of CFB's first 44 graded picks were games decided by
21+ points, where it went 20-1 at prices around -3000. Capturing the line is
what makes the second question answerable.

THE CAPTURE IS ONE-SHOT AND UNREPEATABLE. ESPN publishes an odds block only
while a game is unplayed -- verified against the live feed: the 2026-09-19
scoreboard carried lines for 54 of 71 events, the 2026-09-12 one for 0 of 80.
So a price missed before kickoff is missed forever, and the rule that a rebuild
must never overwrite a stored price with None is not a nicety; it is the
difference between a priced record and an empty column.

NO NETWORK. Every fixture below is a trimmed copy of a real ESPN payload.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import espn_odds  # noqa: E402
import generate_insights  # noqa: E402
import nfl_odds_backtest  # noqa: E402

checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


# ------------------------------------------------------------ conversions
for american, dec in ((100, 2.0), (-100, 2.0), (150, 2.5), (-180, 1.0 + 100 / 180.0),
                      (-8000, 1.0125), ("+164", 2.64), ("-110", 1.0 + 100 / 110.0)):
    got = espn_odds.american_to_decimal(american)
    ok("{} -> decimal {:.4f}".format(american, dec), abs(got - dec) < 1e-9, got)

# Break-even is the RAW implied probability, vig included -- it answers "what
# must I clear at this price", which is the question the Signal Score cannot.
for american, be in ((100, 0.5), (-180, 0.6429), (-8000, 0.9877), (-50000, 0.998), (1400, 0.0667)):
    got = espn_odds.break_even(american)
    ok("{} needs {:.1%}".format(american, be), abs(got - be) < 5e-4, got)

# THE PRICES THAT MADE THE CASE, kept as literals so the argument in the module
# docstring stays checkable: a 61-scored pick priced at -50000 needs 998 wins
# in 1000, and the model's own scale says nothing about that.
ok("a -50000 favourite needs over 99.5%", espn_odds.break_even(-50000) > 0.995)
ok("  while a +164 underdog needs under 40%", espn_odds.break_even(164) < 0.40)

# Junk is None, never a number -- an unparseable price must drop the row out of
# any ROI rather than quietly settle it at evens.
for junk in (None, "", "NA", "n/a", 0, "0", "abc", [], {}):
    ok("{!r} has no decimal".format(junk), espn_odds.american_to_decimal(junk) is None)
    ok("  and no break-even".format(junk), espn_odds.break_even(junk) is None)

# The backtest script carries its own copy (it must not import the live path).
# They are checked against each other rather than trusted to stay in step.
for american in (-8000, -180, -110, 100, 150, 1400):
    ok("espn_odds and nfl_odds_backtest agree on {}".format(american),
       abs(espn_odds.american_to_decimal(american)
           - nfl_odds_backtest.american_to_decimal(american)) < 1e-12)


# ---------------------------------------------------------------- parsing
# A trimmed real event: ESPN nests the moneyline under
# moneyline -> {home,away} -> {open,close} -> odds, as a STRING.
EVENT = {"id": "401869940", "competitions": [{"odds": [{
    "provider": {"name": "DraftKings"}, "details": "DEL -4.5", "spread": -4.5,
    "moneyline": {"home": {"close": {"odds": "-180"}, "open": {"odds": "-175"}},
                  "away": {"close": {"odds": "+150"}, "open": {"odds": "+145"}}}}]}]}
p = espn_odds.parse_event(EVENT)
ok("a real event parses", bool(p), p)
ok("  home moneyline as a signed int", (p or {}).get("home_ml") == -180, p)
ok("  away moneyline, plus sign stripped", (p or {}).get("away_ml") == 150, p)
ok("  the spread comes through", (p or {}).get("spread") == -4.5, p)
ok("  and the book is named, not averaged into a consensus", p["provider"] == "DraftKings")
ok("  capture time is stamped", str(p["captured_at"]).endswith("Z"), p)
# CLOSE, not open: the last published price before kickoff is the closing line,
# and re-reading it every run is what makes the stored value converge on it.
ok("the CLOSE price is taken, not the open", (p["home_ml"], p["away_ml"]) == (-180, 150))

# HALF A PRICE IS WORSE THAN NONE -- it would settle one side at a real number
# and the other at nothing, silently.
half = {"id": "1", "competitions": [{"odds": [{"provider": {"name": "X"},
        "moneyline": {"home": {"close": {"odds": "-180"}}}}]}]}
ok("a one-sided moneyline is rejected outright", espn_odds.parse_event(half) is None)
spread_only = {"id": "1", "competitions": [{"odds": [{"provider": {"name": "X"}, "spread": -3}]}]}
ok("a spread-only block is not a moneyline", espn_odds.parse_event(spread_only) is None)
for junk in ({}, {"competitions": []}, {"competitions": [{}]}, {"competitions": [{"odds": []}]}):
    ok("feed-shaped junk parses to None, never raises", espn_odds.parse_event(junk) is None)


# --------------------------------------------------------------- for_side
ODDS = {"home_ml": -180, "away_ml": 150}
ok("the home side gets the home price", espn_odds.for_side(ODDS, "DEL", "DEL", "CCU") == -180)
ok("the away side gets the away price", espn_odds.for_side(ODDS, "CCU", "DEL", "CCU") == 150)
# EPL's double_chance side reads "<team> or Draw", so an equality test would
# price nothing at all on that market. The leading token is the rule, the same
# one web/insights uses to tint a row.
ok("a 'ARS or Draw' side matches on its leading token",
   espn_odds.for_side({"home_ml": -110, "away_ml": 260}, "ARS or Draw", "SUN", "ARS") == 260)
ok("an unrecognised side prices nothing",
   espn_odds.for_side(ODDS, "DRAW", "DEL", "CCU") is None)
ok("no odds prices nothing", espn_odds.for_side(None, "DEL", "DEL", "CCU") is None)
ok("no side prices nothing", espn_odds.for_side(ODDS, None, "DEL", "CCU") is None)


# ------------------------------------------------------- attach + id mapping
class _FakeSession(object):
    """Returns one scoreboard payload, then records that it was asked."""

    def __init__(self, events, fail=False):
        self.events, self.fail, self.calls = events, fail, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("book outage")
        return _FakeResp({"events": self.events})


class _FakeResp(object):
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


ents = {"401869940": {"away": {"abbr": "CCU"}, "home": {"abbr": "DEL"},
                      "standout": {"side": "DEL", "score": 70}},
        "nope": {"away": {"abbr": "A"}, "home": {"abbr": "B"},
                 "standout": {"side": "B", "score": 60}}}
n = espn_odds.attach(_FakeSession([EVENT]), "cfb", ents, ["2026-09-19"])
ok("attach prices the event it found",
   n == 1 and (ents["401869940"].get("odds") or {}).get("home_ml") == -180,
   ents["401869940"].get("odds"))
ok("  and leaves an unmatched entity with NO odds key, not a null one",
   "odds" not in ents["nope"])

# NFL keys its store by nflverse game_id, so it passes games.csv's `espn` column
# as the mapping -- the same join nfl_grading uses to grade at all.
nfl_ents = {"2026_01_CCU_DEL": {"away": {"abbr": "CCU"}, "home": {"abbr": "DEL"},
                                "standout": {"side": "DEL", "score": 70}}}
espn_odds.attach(_FakeSession([EVENT]), "nfl", nfl_ents, ["2026-09-19"],
                 espn_ids={"2026_01_CCU_DEL": "401869940"})
ok("an espn_ids mapping prices a differently-keyed store",
   (nfl_ents["2026_01_CCU_DEL"].get("odds") or {}).get("home_ml") == -180,
   nfl_ents["2026_01_CCU_DEL"].get("odds"))

# A BOOK OUTAGE MUST COST THE RUN NOTHING. The slate is already built and
# already scoreable; a price is layered on top of it.
clean = {"x": {"away": {"abbr": "A"}, "home": {"abbr": "B"}, "standout": {"side": "B", "score": 1}}}
ok("a failing feed returns 0 and never raises",
   espn_odds.attach(_FakeSession([], fail=True), "cfb", clean, ["2026-09-19"]) == 0)
ok("  leaving every entity unpriced but intact", "odds" not in clean["x"])
ok("an unknown sport fetches nothing", espn_odds.fetch_moneylines(_FakeSession([]), "nhl", ["2026-09-19"]) == {})
ok("no entities means no request at all", espn_odds.attach(_FakeSession([]), "cfb", {}, ["x"]) == 0)

# ONE REQUEST PER DATE, not per game -- the whole reason this rides the
# scoreboard rather than the per-event summary endpoint.
sess = _FakeSession([EVENT])
espn_odds.fetch_moneylines(sess, "cfb", ["2026-09-19", "2026-09-19", "2026-09-18", None, ""])
ok("dates are de-duplicated into one request each", len(sess.calls) == 2, sess.calls)
ok("  and CFB scopes to the FBS group", all(c.get("groups") == 80 for c in sess.calls), sess.calls)
sess = _FakeSession([EVENT])
espn_odds.fetch_moneylines(sess, "nfl", ["2026-09-19"])
ok("  while NFL sends no group filter", "groups" not in sess.calls[0], sess.calls)


# ------------------------------------------------- THE STICKY-PRICE RULE
# The rule the whole feature rests on. ESPN drops the odds block at kickoff, so
# the run that grades is always a run that can no longer see the line. A plain
# rebuild would blank every price at exactly the moment it becomes worth having.
BASE = {"away": {"abbr": "A"}, "home": {"abbr": "B"}, "start": "1:00 PM ET",
        "venue": None, "probables": None, "signals": [], "pulse": None,
        "betting_signals": {}, "standout": {"side": "B", "score": 70}, "status": "Preview"}
PRICED = dict(BASE, odds={"home_ml": -180, "away_ml": 150, "provider": "DraftKings"})

kept = generate_insights._carry_forward_games_store(
    {"g1": dict(BASE)},                      # today's rebuild: no price (kicked off)
    {"g1": {"odds": {"home_ml": -180, "away_ml": 150}}},   # yesterday's store: priced
    "2026-09-19T12:00:00Z", "2026-09-19")
ok("a rebuild with no price KEEPS the stored one",
   (kept["g1"].get("odds") or {}).get("home_ml") == -180, kept["g1"].get("odds"))

fresh = generate_insights._carry_forward_games_store(
    {"g1": PRICED}, {"g1": {"odds": {"home_ml": -150, "away_ml": 130}}},
    "2026-09-19T12:00:00Z", "2026-09-19")
ok("  but a fresh price REPLACES it -- the last one before kickoff is the close",
   (fresh["g1"].get("odds") or {}).get("home_ml") == -180, fresh["g1"].get("odds"))

none_ever = generate_insights._carry_forward_games_store(
    {"g1": dict(BASE)}, {}, "2026-09-19T12:00:00Z", "2026-09-19")
ok("  and a game nobody ever priced stores a null price, not a crash",
   none_ever["g1"]["odds"] is None, none_ever)

# The model's own answer is NOT sticky -- it is rebuilt every run, unlike the
# price. Pinned so the two never get conflated.
moved = generate_insights._carry_forward_games_store(
    {"g1": dict(BASE, standout={"side": "A", "score": 40})},
    {"g1": {"standout": {"side": "B", "score": 99}, "odds": {"home_ml": -180, "away_ml": 150}}},
    "2026-09-19T12:00:00Z", "2026-09-19")
ok("the LEAN is rebuilt even while the price is kept",
   (moved["g1"].get("standout") or {}).get("score") == 40
   and (moved["g1"].get("odds") or {}).get("home_ml") == -180,
   (moved["g1"].get("standout"), moved["g1"].get("odds")))


# ------------------------------------------------------------- card block
blk = generate_insights._price_block(PRICED)
ok("the card block resolves the picked side's price", (blk or {}).get("american") == -180, blk)
ok("  with a signed display string", (blk or {}).get("display") == "-180", blk)
ok("  and the break-even the Signal Score cannot see",
   (blk or {}).get("break_even_display") == "64%", blk)
ok("an unpriced game has no card block", generate_insights._price_block(dict(BASE)) is None)
ok("a game with no lean has no card block",
   generate_insights._price_block(dict(PRICED, standout=None)) is None)
ok("a side the book does not quote has no card block",
   generate_insights._price_block(dict(PRICED, standout={"side": "DRAW", "score": 70})) is None)

# ------------------------------------------------- the priced standing record
# The line that answers "did it make money", which no hit rate in this repo
# does. Flat one-unit stakes: the only staking plan that does not smuggle a
# second model in beside the first.
import signal_report  # noqa: E402

ok("nothing priced yet says NOTHING, rather than printing a 0.0% ROI",
   signal_report.priced_record_lines(
       [{"verdict": "HIT", "price": None}, {"verdict": "MISS"}]) == [])

# THE POINT OF THE WHOLE FEATURE, in two records with the SAME 66.7% hit rate
# and opposite outcomes. Only the price separates them, and only this line can
# see it -- the Signal Score cannot.
#
# At -180 (needs 64.3%) a win returns 1.5556 per unit staked, so 2-1 is 3.1111
# back on 3 -> +3.7%: a real if thin edge.
win = signal_report.priced_record_lines(
    [{"verdict": "HIT", "price": -180, "break_even": 0.6429},
     {"verdict": "HIT", "price": -180, "break_even": 0.6429},
     {"verdict": "MISS", "price": -180, "break_even": 0.6429}])[0]
ok("2-1 at -180 clears its break-even and shows a profit", "+3.7%" in win, win)
ok("  reporting the hit rate beside it", "66.7%" in win, win)
ok("  and what that price NEEDED", "64.3%" in win, win)

# At -250 (needs 71.4%) the very same 2-1 returns 2.8 on 3 -> -6.7%. A winning
# record, a losing bet. This is the CFB finding in miniature: 77% right at
# prices around -3000 is not an edge, and a hit rate alone cannot say so.
lose = signal_report.priced_record_lines(
    [{"verdict": "HIT", "price": -250, "break_even": 0.7143},
     {"verdict": "HIT", "price": -250, "break_even": 0.7143},
     {"verdict": "MISS", "price": -250, "break_even": 0.7143}])[0]
ok("the SAME 2-1 record at -250 is a LOSS", "-6.7%" in lose, lose)
ok("  even though the hit rate is identical", "66.7%" in lose, lose)
ok("  because the price needed more than it got", "71.4%" in lose, lose)

# A push returns the stake -- it is neither a win nor a loss, and counting it as
# either would misreport the record.
push = signal_report.priced_record_lines(
    [{"verdict": "PUSH", "price": -110, "break_even": 0.5238}])[0]
ok("a push returns the stake exactly", "+0.0%" in push, push)

# An unpriced pick SITS OUT rather than counting as a loss: it was a real pick,
# just not one this record can price.
mixed = signal_report.priced_record_lines(
    [{"verdict": "HIT", "price": 150, "break_even": 0.40},
     {"verdict": "MISS", "price": None}, {"verdict": "HIT", "price": None}])
ok("unpriced picks are excluded, not counted as losses", "+150.0%" in mixed[0], mixed)
ok("  and their exclusion is stated out loud", "2 graded picks carry no price" in mixed[1], mixed)

# A row whose grade is not a settlement (UNRESOLVED, POSTPONED) never reaches
# the stake count, whatever price it carries.
ok("an unresolved row is not staked",
   signal_report.priced_record_lines(
       [{"verdict": "UNRESOLVED", "price": -110}]) == [])


print("odds: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "odds: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
