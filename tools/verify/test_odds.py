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

ok("  a quoted market is not flagged off", p["moneyline_off"] is False, p)

# HALF A PRICE IS WORSE THAN NONE -- it would settle one side at a real number
# and the other at nothing, silently. Neither side is kept, but the BLOCK is:
# its spread is what says how lopsided the game was.
half = {"id": "1", "competitions": [{"odds": [{"provider": {"name": "X"}, "spread": -3,
        "moneyline": {"home": {"close": {"odds": "-180"}}}}]}]}
hp = espn_odds.parse_event(half)
ok("a one-sided moneyline yields no price at all", hp["home_ml"] is None and hp["away_ml"] is None, hp)
ok("  but the spread survives", hp["spread"] == -3.0, hp)
spread_only = {"id": "1", "competitions": [{"odds": [{"provider": {"name": "X"}, "spread": -3}]}]}
ok("a spread-only block carries no moneyline",
   (espn_odds.parse_event(spread_only) or {}).get("home_ml") is None)
for junk in ({}, {"competitions": []}, {"competitions": [{}]}, {"competitions": [{"odds": []}]}):
    ok("NO odds block at all parses to None -- nothing known", espn_odds.parse_event(junk) is None)


# --------------------------------------------------- OFF is not "missing"
# THE WORST BUG THIS MODULE HAD. ESPN prints the literal string "OFF" when a
# book has PULLED a market rather than never offered one -- 10 of the 142
# moneyline cells on the 2026-09-19 board, every one behind a spread of 36 to
# 51.5 points. Read as missing data, those games sailed through the
# bettability filter untouched and were PROMOTED to the top of the board the
# filter had just cleared, which is exactly backwards: a market the book
# refuses to quote is more unbettable than -8000, not less.
OFF_EVENT = {"id": "9", "competitions": [{"odds": [{
    "provider": {"name": "DraftKings"}, "details": "IU -44.5", "spread": -44.5,
    "moneyline": {"home": {"close": {"odds": "OFF"}},
                  "away": {"close": {"odds": "OFF"}}}}]}]}
off = espn_odds.parse_event(OFF_EVENT)
ok("an OFF market still parses to a block", bool(off), off)
ok("  flagged as pulled, not absent", off["moneyline_off"] is True, off)
ok("  with no price on either side", off["home_ml"] is None and off["away_ml"] is None, off)
ok("  and the spread kept, which is what says how lopsided it was",
   off["spread"] == -44.5, off)
ok("  lowercase 'off' counts too", espn_odds.parse_event(
    {"id": "9", "competitions": [{"odds": [{"moneyline": {
        "home": {"close": {"odds": "off"}}, "away": {"close": {"odds": "off"}}}}]}]})["moneyline_off"] is True)
ok("  one side OFF is enough -- the market is off", espn_odds.parse_event(
    {"id": "9", "competitions": [{"odds": [{"moneyline": {
        "home": {"close": {"odds": "OFF"}}, "away": {"close": {"odds": "+2200"}}}}]}]})["moneyline_off"] is True)


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

# ---------------------------------------------- the card, in plain words
# The 2026-09-25 redesign: labelled rows instead of "-325 needs 76% from -142
# +17.8pp". Three REAL games from that day's NFL scoreboard (DraftKings), trimmed
# to the moneyline and pointSpread cells parse_event reads, each chosen for the
# branch it exercises:
#   LAC @ BUF  the card the owner asked about: moved toward the home pick
#   MIN @ TB   the favourite CHANGED SIDES (TB -1.5 -> MIN -1.5), which the old
#              home-relative "line -1.5 → +1.5" hid behind a sign convention
#   LV @ NO    the spread never moved, so no "(opened ...)" is printed
# Sabotage-checked when written: naming the away team for a negative line fails
# the two favourite-named assertions (2 of 160); printing "(opened ...)" for an
# unmoved line fails exactly LV @ NO (1); swapping the direction words fails the
# BUF and TB wording (2). In the browser suite, dropping the Market row fails
# its four assertions (184 passed, 4 failed).
def _nfl_event(details, total, ml, ps):
    cell = lambda o, c, k: {"open": {k: o}, "close": {k: c}}
    return {"competitions": [{"odds": [{
        "provider": {"name": "DraftKings"}, "details": details, "overUnder": total,
        "moneyline": {"home": cell(ml[0], ml[1], "odds"), "away": cell(ml[2], ml[3], "odds")},
        "pointSpread": {"home": cell(ps[0], ps[1], "line")}}]}]}


def _card(event, away, home, side):
    return generate_insights._price_block({
        "away": {"abbr": away}, "home": {"abbr": home},
        "odds": espn_odds.parse_event(event),
        "standout": {"side": side, "bet_type": "moneyline", "score": 89}})


buf = _card(_nfl_event("BUF -7", 50.5, ("-142", "-325", "+120", "+260"), ("-2.5", "-7")),
            "LAC", "BUF", "BUF")
ok("BUF card: the move is both prices, open then now",
   (buf or {}).get("market_display") == "-142 → -325", buf)
ok("  named for the picked team", (buf or {}).get("move_text") == "moved toward BUF", buf)
ok("  the spread says where it opened, favourite named",
   (buf or {}).get("spread_text") == "BUF -7 (opened BUF -2.5)", buf)
ok("  and the measured pp figure is still in the payload for the ledger",
   (buf or {}).get("move_display") == "+17.8pp", buf)

tb = _card(_nfl_event("MIN -1.5", 42.5, ("-125", "+102", "+105", "-122"), ("-1.5", "+1.5")),
           "MIN", "TB", "TB")
ok("MIN @ TB: a favourite that changed sides reads as two teams, not two signs",
   (tb or {}).get("spread_text") == "MIN -1.5 (opened TB -1.5)", tb)
ok("  and a pick the market left is said so",
   (tb or {}).get("move_text") == "moved away from TB", tb)

no = _card(_nfl_event("NO -3", 43.5, ("-166", "-185", "+140", "+154"), ("-3", "-3")),
           "LV", "NO", "NO")
ok("LV @ NO: an unmoved spread prints no '(opened ...)'",
   (no or {}).get("spread_text") == "NO -3", no)

ok("favored_line: a pick'em is a pick'em", espn_odds.favored_line(0.0, "A", "B") == "pick'em")
ok("  and no line is no text", espn_odds.favored_line(None, "A", "B") is None)


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


# --------------------------------------------------- the bettability filter
# A Signal Score cannot see price, which makes the TOP of a college board
# systematically its LEAST bettable part: on 2026-09-19 the three
# highest-scored picks were quoted -4000, -8000 and -3200. The cap is the
# model's own ceiling -- its best-ever measured band, scores of 90+, went 10-1
# live, which is 90.9%, which is exactly -1000.
CAP = 0.9091   # == American -1000 exactly (10/11)


def _ent(side="HME", score=80, odds=None):
    e = {"away": {"abbr": "AWY"}, "home": {"abbr": "HME"},
         "standout": {"side": side, "score": score, "bet_type": "moneyline"},
         "best_angle": {"side": side, "score": score},
         "betting_signals": {"moneyline": {"side": side, "score": score}},
         "signal_scores": [{"market": "Moneyline", "side": side, "score": score}]}
    if odds is not None:
        e["odds"] = odds
    return e


ok("a price inside the cap is untouched",
   espn_odds.unbettable(-500, CAP) == (False, espn_odds.break_even(-500)))
ok("a price past it is unbettable", espn_odds.unbettable(-8000, CAP)[0] is True)
# THE BOUNDARY IS THE MEASUREMENT. 10/11 is both the model's best-ever band
# (10-1 at scores of 90+) and the break-even of -1000, so a -1000 line is
# admitted and anything worse is not. Set as the fraction rather than a rounded
# 0.909, which would have excluded -1000 itself by four ten-thousandths.
ok("  and the cap lands exactly on the model's ceiling",
   espn_odds.unbettable(-1000, CAP)[0] is False
   and espn_odds.unbettable(-1001, CAP)[0] is True,
   (espn_odds.break_even(-1000), CAP))
ok("  a rounded 0.909 would have excluded -1000 itself",
   espn_odds.unbettable(-1000, 0.909)[0] is True)

# UNPRICED IS NOT UNBETTABLE, and conflating them is the failure mode worth
# naming: ESPN publishes no market at all for a chunk of every board, mostly
# the smaller programs -- exactly the part least likely to be efficiently
# priced. Dropping those would be the opposite of what this filter is for.
ok("no price is NOT unbettable -- unknown is not the same as unbackable",
   espn_odds.unbettable(None, CAP) == (False, None))
ok("no cap configured disables the filter entirely",
   espn_odds.unbettable(-50000, None) == (False, espn_odds.break_even(-50000)))

# Suppression removes the BET, never the analysis.
ents = {"a": _ent(odds={"home_ml": -8000, "away_ml": 2000}),
        "b": _ent(odds={"home_ml": -150, "away_ml": 130}),
        "c": _ent()}                      # unpriced
cut = espn_odds.apply_bettability(ents, CAP, "cfb")
ok("the -8000 pick is suppressed", cut == 1 and ents["a"]["standout"] is None, ents["a"])
ok("  and its best_angle with it", ents["a"]["best_angle"] is None)
ok("  but the OPINION survives -- signal_scores untouched",
   [r["score"] for r in ents["a"].get("signal_scores") or []] == [80],
   ents["a"].get("signal_scores"))
ok("  and betting_signals untouched",
   ((ents["a"].get("betting_signals") or {}).get("moneyline") or {}).get("score") == 80,
   ents["a"].get("betting_signals"))
ok("  with a reason the card can render",
   ents["a"]["no_bet"]["reason"] == "price"
   and ents["a"]["no_bet"]["display"] == "-8000"
   and ents["a"]["no_bet"]["break_even_display"] == "99%", ents["a"].get("no_bet"))
ok("a bettable pick keeps its standout", ents["b"]["standout"] is not None)
ok("  and gets no no_bet block", "no_bet" not in ents["b"])
ok("an UNPRICED pick is left alone", ents["c"]["standout"] is not None, ents["c"])

# A MARKET THE BOOK PULLED outranks any price for unbettability.
off_ent = {"o": _ent(odds={"home_ml": None, "away_ml": None, "moneyline_off": True,
                           "details": "IU -44.5"})}
espn_odds.apply_bettability(off_ent, CAP, "cfb")
ok("an OFF market suppresses the pick", off_ent["o"]["standout"] is None, off_ent["o"])
ok("  labelled as pulled, not as a price",
   off_ent["o"]["no_bet"]["reason"] == "off_the_board"
   and off_ent["o"]["no_bet"]["display"] == "OFF", off_ent["o"].get("no_bet"))
ok("  carrying the spread that explains it",
   off_ent["o"]["no_bet"]["spread"] == "IU -44.5", off_ent["o"].get("no_bet"))
ok("  and no break-even, because there is no price",
   off_ent["o"]["no_bet"]["break_even"] is None)

# The side the pick took is what gets priced, not the favourite.
dog = {"d": _ent(side="AWY", odds={"home_ml": -8000, "away_ml": 2000})}
espn_odds.apply_bettability(dog, CAP, "cfb")
ok("backing the +2000 dog is bettable even when the other side is -8000",
   dog["d"]["standout"] is not None, dog["d"])

# A game with no lean at all has nothing to suppress.
none_ent = {"n": {"away": {"abbr": "A"}, "home": {"abbr": "B"}, "standout": None}}
ok("a game with no pick is a no-op",
   espn_odds.apply_bettability(none_ent, CAP, "cfb") == 0)
ok("no cap means no suppression anywhere",
   espn_odds.apply_bettability({"a": _ent(odds={"home_ml": -50000, "away_ml": 9000})},
                               None, "cfb") == 0)


# ------------------------------------------------------- closing-line value
# THE MEASUREMENT THAT REPLACED A SPREAD MODEL. Three walk-forward tests over
# 2010-2025 NFL (~7,000 games with real closing spreads) said this repo's
# signals cannot out-predict a line: margin gap explains R2=0.0001 of its
# residual, all six signals jointly R2=0.0027 in-sample, and the unused columns
# are priced too (0 of 28 referee crews deviate at 2se where chance predicts
# ~1). ATS came out 50.96% on 2,560 out-of-sample bets against 52.4%.
#
# So the question became "does the market move toward our picks at all", which
# is CLV -- and it converges in weeks rather than a season.
MOVED = {"home_ml": 180, "away_ml": -218, "home_ml_open": 210, "away_ml_open": -258}

toward = espn_odds.clv(MOVED, "HME", "HME", "AWY")
away = espn_odds.clv(MOVED, "AWY", "HME", "AWY")
ok("a price that SHORTENED moved toward the pick",
   toward["direction"] == "toward" and toward["delta"] > 0, toward)
ok("  measured in probability points, not raw American odds",
   toward["delta_display"] == "+3.5pp", toward)
ok("  carrying both ends so the move is auditable",
   (toward["open"], toward["close"]) == (210, 180), toward)
ok("the other side of the same game moved the OTHER WAY",
   toward["delta"] > 0 > away["delta"], (toward["delta"], away["delta"]))
ok("  and is labelled as such", away["direction"] == "away", away)
# NOT EXACTLY EQUAL AND OPPOSITE, and that is correct rather than sloppy.
# break_even() is the RAW implied probability, vig included, so the two sides
# sum to the book's hold (1.0433 at the open here, 1.0427 at the close) rather
# than to 1. The residual is the hold's own drift. Asserting exact symmetry
# would be asserting a de-vigged number this deliberately does not compute --
# because the question is "what must I clear at this price", and the vig is
# part of that.
ok("  differing only by the book's own hold drift",
   abs(toward["delta"] + away["delta"]) < 0.001, toward["delta"] + away["delta"])

# PROBABILITY POINTS ARE THE POINT. American odds are not linear -- -110 to
# -130 and +200 to +180 are wildly different in cents and comparable in
# probability -- so summing raw American deltas across a board would be
# meaningless arithmetic. Pinned with a case that makes it obvious.
short_fav = espn_odds.clv({"home_ml": -130, "away_ml": 110,
                           "home_ml_open": -110, "away_ml_open": -110}, "HME", "HME", "AWY")
long_dog = espn_odds.clv({"home_ml": 180, "away_ml": -218,
                          "home_ml_open": 200, "away_ml_open": -258}, "HME", "HME", "AWY")
ok("a 20-cent favourite move and a 20-cent dog move are NOT the same move",
   abs(short_fav["delta"] - long_dog["delta"]) > 0.01,
   (short_fav["delta_display"], long_dog["delta_display"]))

flat = espn_odds.clv({"home_ml": -150, "away_ml": 130,
                      "home_ml_open": -150, "away_ml_open": 130}, "HME", "HME", "AWY")
ok("an unmoved line reads flat, not toward", flat["direction"] == "flat" and flat["delta"] == 0.0)

# UNMEASURABLE IS NOT ZERO. A pick with no opening number, no closing number,
# or a market the book pulled is EXCLUDED -- counting it as no movement would
# drag every average toward zero by construction.
ok("no opening number is unmeasurable",
   espn_odds.clv({"home_ml": -150, "away_ml": 130}, "HME", "HME", "AWY") is None)
ok("no closing number is unmeasurable",
   espn_odds.clv({"home_ml_open": -150, "away_ml_open": 130}, "HME", "HME", "AWY") is None)
ok("a pulled market is unmeasurable",
   espn_odds.clv({"home_ml": None, "away_ml": None, "moneyline_off": True,
                  "home_ml_open": None, "away_ml_open": None}, "HME", "HME", "AWY") is None)
ok("no odds at all is unmeasurable", espn_odds.clv(None, "HME", "HME", "AWY") is None)
ok("a side the book does not quote is unmeasurable",
   espn_odds.clv(MOVED, "DRAW", "HME", "AWY") is None)
ok("an 'X or Draw' side still matches on its leading token",
   espn_odds.clv(MOVED, "HME or Draw", "HME", "AWY") is not None)


# ------------------------------------------------------- open/close capture
# ESPN publishes the book's own OPENING number beside the current one, and it
# is a genuinely different value -- 45 of 54 CFB and 13 of 14 NFL games on the
# 2026-09-19/20 boards had open != close. Without it CLV cannot be asked.
OC = {"id": "1", "competitions": [{"odds": [{
    "provider": {"name": "DraftKings"}, "details": "TEX -5.5", "spread": 5.5,
    "overUnder": 57.5,
    "moneyline": {"home": {"close": {"odds": "+180"}, "open": {"odds": "+210"}},
                  "away": {"close": {"odds": "-218"}, "open": {"odds": "-258"}}},
    "pointSpread": {"home": {"close": {"line": "+5.5"}, "open": {"line": "+7.0"}},
                    "away": {"close": {"line": "-5.5"}, "open": {"line": "-7.0"}}}}]}]}
oc = espn_odds.parse_event(OC)
ok("the closing moneyline is captured", (oc["home_ml"], oc["away_ml"]) == (180, -218), oc)
ok("  and the OPENING one alongside it",
   (oc["home_ml_open"], oc["away_ml_open"]) == (210, -258), oc)
ok("the total comes through", oc["total"] == 57.5, oc)
ok("both ends of the spread come through",
   (oc["spread_open"], oc["spread_close"]) == (7.0, 5.5), oc)

# SIGN: ESPN's spread is HOME-RELATIVE and NEGATIVE when the home side lays
# points -- ILL @ OSU reads home_line -27.5. That is the OPPOSITE of nflverse's
# `spread_line`, which is POSITIVE when the home team is favoured and is what
# nfl_odds_backtest reads. Two conventions live in this repo; conflating them
# silently inverts everything built on top, so both are pinned here.
HOME_FAV = {"id": "2", "competitions": [{"odds": [{"details": "OSU -27.5", "spread": -27.5,
    "pointSpread": {"home": {"close": {"line": "-27.5"}, "open": {"line": "-24.5"}}},
    "moneyline": {"home": {"close": {"odds": "-6500"}, "open": {"odds": "-5000"}},
                  "away": {"close": {"odds": "+2000"}, "open": {"odds": "+1600"}}}}]}]}
hf = espn_odds.parse_event(HOME_FAV)
ok("a home favourite's ESPN spread is NEGATIVE", hf["spread_close"] == -27.5, hf)
sm = espn_odds.spread_move(hf)
ok("the spread move is reported home-relative", sm["move"] == -3.0, sm)
ok("  with both ends shown", sm["display"] == "-24.5 → -27.5", sm)
ok("an unmeasurable spread move is None", espn_odds.spread_move({"spread_open": 3}) is None)
ok("no odds means no spread move", espn_odds.spread_move(None) is None)

# The card block carries all of it.
CARD = dict(PRICED, odds=dict(MOVED, spread_open=7.0, spread_close=5.5,
                              total=57.5, details="TEX -5.5", provider="DraftKings"))
blk = generate_insights._price_block(CARD)
# PRICED's standout takes the HOME side, so the card resolves the home price:
# +210 open -> +180 close, a move toward the pick.
ok("the card shows which way the market moved", blk["move_display"] == "+3.5pp", blk)
ok("  naming the direction", blk["move_direction"] == "toward", blk)
ok("  and from what", blk["opened"] == "+210", blk)
ok("  the total", blk["total"] == 57.5, blk)
ok("  and the spread's travel", blk["spread_move"] == "+7 → +5.5", blk)


# ------------------------------------------------- the CLV standing record
ok("no measurable movement says NOTHING, rather than printing 0.0pp",
   signal_report.clv_record_lines([{"verdict": "HIT", "clv_delta": None}]) == [])
line = signal_report.clv_record_lines(
    [{"verdict": "HIT", "clv_delta": 0.03}, {"verdict": "MISS", "clv_delta": 0.01},
     {"verdict": "MISS", "clv_delta": -0.02}, {"verdict": "PUSH", "clv_delta": 0.0}])[0]
ok("the mean movement is reported in probability points", "+0.50pp" in line, line)
ok("  with the share the market moved toward", "2 of 4" in line, line)
ok("  and the unmoved counted separately", "1 unmoved" in line, line)
ok("a thin sample says so out loud, and names the null",
   "the null is 0.0pp" in signal_report.clv_record_lines(
       [{"verdict": "HIT", "clv_delta": 0.01}])[1])
# Ungraded rows are not yet evidence of anything.
ok("a pick with no verdict is not counted",
   signal_report.clv_record_lines([{"clv_delta": 0.05}]) == [])


print("odds: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "odds: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
