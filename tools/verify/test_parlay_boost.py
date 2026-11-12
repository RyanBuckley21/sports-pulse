"""Regression tests for the boosted-parlay arithmetic (parlay_boost.py) and
the market chance bet_board puts on every leg.

Run: python3 -m tools.verify.test_parlay_boost   (from the repo root)

WHY THIS IS PINNED. The Bets tray prints "worth +20.9% per $1" beside a real
ticket. Every way that number can be wrong reads as a reason to bet:

  * THE BOOST MULTIPLIES WINNINGS, NOT THE PAYOUT. +312 at a 50% boost pays
    +468. Multiplying the whole payout gives +518 and overstates every value.
  * THE CHANCE IS THE MARKET'S, WITH THE VIG OUT. Using the raw break-even
    (vig in) as the chance makes every ticket look fair before the boost;
    using one side's price alone cannot remove a margin at all.
  * VIG COMPOUNDS. The break-even boost must rise with every leg added.
  * A PROMO TERM IS A HARD LINE. "-200 per leg" admits -200 and refuses -201;
    "+300 minimum" admits +300. Off by one in either direction tells the
    reader a ticket qualifies when the book will refuse the boost.
  * NO CHANCE IS BETTER THAN A WRONG ONE. Two legs from one game, or a leg
    with a one-sided price, get no probability and no value.
  * NO DEFAULT PROMO, and no score in the price: the module takes every term
    from its caller and never reads a Signal Score.

The board is REAL: tools/verify/bets_fixture.json (14 games from the
committed store at f99baf7), built by the real bet_board.build. The legs used
are CWS -240, CAR -148 and TOR -136, plus BUF -340 and KC -575. Boundary
cases use prices stated in the assertion (a promo line is arithmetic on a
number, not a property of a feed). One edge edits one field: a game whose
OTHER side has no price (822760's away_ml set to None).

Sabotage-checked when written (2026-11-10, PYTHONDONTWRITEBYTECODE=1, sources
byte-identical afterwards), failures measured, of 51:
  P1  boost multiplies the whole payout             5 fail
  P2  chance = raw break-even (vig left in)         8 fail
  P3  a one-sided price accepted                    2 fail
  P4  leg floor refuses the line itself             2 fail
  P5  leg floor compared the wrong way              6 fail
  P6  total minimum excludes the line itself        1 fail
  P7  only the first unmet term reported            1 fail
  P8  same-game legs priced as independent          1 fail
  P9  a leg without a chance treated as certain     1 fail
  P10 break-even boost ignores the stake            2 fail
  P11 min legs off by one                           1 fail
  B1  board gives every leg the HOME side's chance  8 fail
  B2  board uses the break-even as the chance       9 fail
The page repeats the formula in bets.js; tools/verify/run.js pins its numbers
to this module's and was sabotaged six ways of its own (see its README).

WHICH TICKET (section 7, added 2026-11-12, 22 checks, of 73). suggest() names
the tickets for a promo, so a wrong one reads as advice. Sabotage-checked the
same way:
  S1  boost multiplies the whole payout             4 fail
  S2  the +100 leg cap ignored (underdogs as legs)  11 fail
  S3  the promo's leg floor ignored                 1 fail
  S4  the total minimum ignored                     3 fail
  S5  "likeliest" = any ticket above its stake      3 fail
  S6  two legs from one game on a ticket            1 fail
  S7  a leg with no chance treated as certain       1 fail
  S8  a zero boost given a suggestion               1 fail
  S9  the size budget set to 10                     12 fail (nothing is searched)
  S10 "best value" picks the likeliest instead      5 fail
  S12 +100 itself refused                           1 fail
  S13 only the minimum size searched                3 fail
  S11 the positive-value gate on the second pass removed: 0 fail, and
      correctly. The bar is half the best value, so when the best ticket
      loses, nothing can reach half of a loss either. Guarded twice.
"""

import ast
import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import bet_board  # noqa: E402
import parlay_boost as pb  # noqa: E402
import yaml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


def near(a, b, tol=1e-4):
    return a is not None and b is not None and abs(a - b) <= tol


FX = json.load(open(os.path.join(HERE, "bets_fixture.json")))
CONFIG = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))
BOARD = bet_board.build(FX["entities"], CONFIG, [])
ROWS = {l["side"]: l for l in BOARD["parlay"] + BOARD["straight"]}


def legs(*sides):
    return [{"game": ROWS[s]["id"], "decimal": ROWS[s]["decimal"],
             "fair": ROWS[s]["fair"], "label": s} for s in sides if s in ROWS]


def priced(american, fair=None, game=None):
    dec = pb.american_to_decimal(american)
    return {"game": game or "g{}".format(american), "decimal": dec,
            "fair": fair if fair is not None else 1.0 / dec, "label": "{:+d}".format(american)}


# ----------------------------------------------------------- 1. conversions
ok("+300 is decimal 4.0, -200 is 1.5, +100 and -100 are both 2.0",
   (pb.american_to_decimal(300), pb.american_to_decimal(-200),
    pb.american_to_decimal(100), pb.american_to_decimal(-100)) == (4.0, 1.5, 2.0, 2.0))
ok("zero and garbage have no decimal",
   pb.american_to_decimal(0) is None and pb.american_to_decimal("OFF") is None
   and pb.american_to_decimal(None) is None)
ok("decimal back to American, as a book prints it",
   (pb.decimal_to_american(4.0), pb.decimal_to_american(1.5), pb.decimal_to_american(2.0))
   == (300, -200, 100))

# ------------------------------------------------------ 2. the market chance
# BUF -340 against LAC +270 (the fixture's own two prices for that game).
buf_game = FX["entities"][ROWS["BUF"]["id"]]["odds"]
pair = pb.no_vig(buf_game["home_ml"], buf_game["away_ml"])
ok("no-vig: the two sides sum to exactly 1", pair is not None and near(sum(pair), 1.0, 1e-12), pair)
raw = 1 / pb.american_to_decimal(buf_game["home_ml"]) + 1 / pb.american_to_decimal(buf_game["away_ml"])
ok("  the raw break-evens sum to MORE than 1 (that excess is the vig)", raw > 1.02, raw)
ok("  BUF's market chance is its break-even scaled down by that excess",
   near(ROWS["BUF"]["fair"], (1 / ROWS["BUF"]["decimal"]) / raw, 1e-6), (ROWS["BUF"]["fair"], raw))
ok("  one side alone gives no chance", pb.no_vig(-340, None) is None and pb.no_vig(None, 270) is None)
ok("  nor does an OFF line", pb.no_vig("OFF", "OFF") is None)
ok("every board leg carries a market chance between 0 and 1",
   bool(ROWS) and all(l["fair"] is not None and 0 < l["fair"] < 1 for l in ROWS.values()),
   {s: l["fair"] for s, l in ROWS.items()})
ok("  and it is always BELOW the price's break-even (the vig is out)",
   all(l["fair"] < l["break_even"] for l in ROWS.values()),
   {s: (l["fair"], l["break_even"]) for s, l in ROWS.items()})
ok("  the picked side gets ITS chance, home or away",
   all(near(l["fair"], pb.fair_prob(FX["entities"][l["id"]]["odds"], l["at_home"]), 1e-6)
       for l in ROWS.values()))
ok("  a favourite's chance is above a half and an underdog's below",
   ROWS["KC"]["fair"] > 0.5 > ROWS["APP"]["fair"], (ROWS["KC"]["fair"], ROWS["APP"]["fair"]))
# EDGE, one field edited: the OTHER side of TOR's game has no price.
half = copy.deepcopy(FX["entities"])
half["822760"]["odds"]["away_ml"] = None
half_rows = {l["side"]: l for b in [bet_board.build(half, CONFIG, [])]
             for l in b["parlay"] + b["straight"]}
ok("a leg whose other side is unpriced keeps its row but has NO market chance",
   "TOR" in half_rows and half_rows["TOR"]["fair"] is None, half_rows.get("TOR"))

# ------------------------------------------------------- 3. the promo terms
ok("no terms at all: any ticket qualifies", pb.check(legs("CWS", "CAR"), {}) == []
   and pb.check(legs("CWS"), None) == [])
ok("min legs: 2 of 3 is refused, 3 of 3 passes",
   pb.check(legs("CWS", "CAR"), {"min_legs": 3}) == ["needs 3+ legs"]
   and pb.check(legs("CWS", "CAR", "TOR"), {"min_legs": 3}) == [])
ok("leg floor -200 refuses the -240 leg, by name",
   pb.check(legs("CWS", "CAR", "TOR"), {"leg_floor_american": -200}) == ["CWS is shorter than -200"])
ok("  and -250 admits it", pb.check(legs("CWS", "CAR", "TOR"), {"leg_floor_american": -250}) == [])
ok("  the line itself qualifies: -200 against a -200 floor",
   pb.check([priced(-200)], {"leg_floor_american": -200}) == [])
ok("  one tick shorter does not: -201", pb.check([priced(-201)], {"leg_floor_american": -200}) != [])
ok("  an underdog always clears a favourite's floor",
   pb.check([priced(120)], {"leg_floor_american": -200}) == [])
ok("  a +100 floor refuses every favourite and admits even money",
   pb.check([priced(-110)], {"leg_floor_american": 100}) != []
   and pb.check([priced(100)], {"leg_floor_american": 100}) == []
   and pb.check([priced(-100)], {"leg_floor_american": 100}) == [])
three = legs("CWS", "CAR", "TOR")                      # +312
ok("total: +312 clears a +300 minimum and fails a +400 one",
   pb.check(three, {"min_total_american": 300}) == []
   and pb.check(three, {"min_total_american": 400}) == ["pays under +400"])
ok("  exactly +300 clears +300 (two +100 legs)",
   pb.check([priced(100, game="a"), priced(100, game="b")], {"min_total_american": 300}) == [])
ok("  +100 minimum on the whole ticket: a single -150 fails, two of them pass",
   pb.check([priced(-150)], {"min_total_american": 100}) != []
   and pb.check([priced(-150, game="a"), priced(-150, game="b")], {"min_total_american": 100}) == [])
allbad = pb.check(legs("BUF", "KC"), {"min_legs": 3, "min_total_american": 300, "leg_floor_american": -200})
ok("every unmet term is listed, not just the first", len(allbad) == 4, allbad)

# ----------------------------------------------------------- 4. the value
r = pb.evaluate(three, {"boost_pct": 50})
ok("CWS+CAR+TOR: +312, market chance 21.3%",
   r["american"] == 312 and near(r["win_prob"], 0.2130, 5e-4), r)
ok("  the chance is the PRODUCT of the legs' market chances",
   near(r["win_prob"], ROWS["CWS"]["fair"] * ROWS["CAR"]["fair"] * ROWS["TOR"]["fair"], 1e-9), r)
ok("  unboosted it is worth -12.3% (the vig, compounded)", near(r["ev_unboosted"], -0.1227, 5e-4), r)
ok("  a 50% boost multiplies the WINNINGS: +312 pays +468, not +518",
   r["boosted_american"] == 468, r["boosted_american"])
ok("  and is worth +20.9% per unit", near(r["ev_boosted"], 0.2094, 5e-4), r)
P, D = r["win_prob"], r["decimal"]
ok("  which is P*(1 + (D-1)*1.5) - 1, recomputed here",
   near(r["ev_boosted"], P * (1 + (D - 1) * 1.5) - 1, 1e-12))
ok("  break-even boost 18.47%", near(r["break_even_boost"], 0.1847, 5e-4), r)
at = pb.evaluate(three, {"boost_pct": 100 * r["break_even_boost"]})
ok("  at exactly that boost the ticket is worth exactly its stake", near(at["ev_boosted"], 0.0, 1e-9), at)
zero = pb.evaluate(three, {"boost_pct": 0})
ok("  a 0% boost changes nothing", near(zero["ev_boosted"], zero["ev_unboosted"], 1e-12)
   and zero["boosted_american"] == zero["american"], zero)
none = pb.evaluate(three, {})
ok("no boost entered: no boosted price and no boosted value, but the rest stands",
   none["boost"] is None and none["ev_boosted"] is None and none["boosted_american"] is None
   and none["priceable"] and near(none["ev_unboosted"], r["ev_unboosted"], 1e-12), none)

# Vig compounds: each extra leg needs a bigger boost.
needs = [pb.evaluate(legs(*sides))["break_even_boost"]
         for sides in (("CWS",), ("CWS", "CAR"), ("CWS", "CAR", "TOR"), ("CWS", "CAR", "TOR", "BUF"))]
ok("the break-even boost RISES with every leg added", needs == sorted(needs) and len(set(needs)) == 4, needs)
four = [priced(-200, fair=0.96 / 1.5, game=g) for g in "abcd"]
ok("four legs each returning 0.96 return 0.849 together",
   near(pb.evaluate(four)["ev_unboosted"], 0.96 ** 4 - 1, 1e-9))
fair2 = [priced(-200, game="a"), priced(150, game="b")]
f = pb.evaluate(fair2, {"boost_pct": 30})
ok("fair-priced legs are worth exactly their stake and need no boost",
   near(f["ev_unboosted"], 0.0, 1e-12) and near(f["break_even_boost"], 0.0, 1e-12), f)
ok("  and on them the boost's whole value is b * P * (D - 1)",
   near(f["ev_boosted"], 0.30 * f["win_prob"] * (f["decimal"] - 1), 1e-12), f)

# ------------------------------------------------- 5. when there is no chance
same = [priced(-150, game="g1"), priced(-120, game="g1")]
s = pb.evaluate(same, {"boost_pct": 50})
ok("two legs from ONE game: priced as a ticket, but given no chance and no value",
   s["american"] is not None and not s["priceable"] and s["win_prob"] is None
   and s["ev_boosted"] is None and "correlated" in (s["why"] or ""), s)
nofair = legs("CWS", "CAR")
nofair[1]["fair"] = None
n = pb.evaluate(nofair, {"boost_pct": 50})
ok("a leg with no market chance: no probability and no value for the ticket",
   not n["priceable"] and n["win_prob"] is None and n["ev_boosted"] is None, n)
ok("  but the promo terms are still checked", n["qualifies"] is True and n["reasons"] == [], n)
e = pb.evaluate([], {"boost_pct": 50})
ok("no legs: nothing is priced and nothing throws", not e["priceable"] and e["decimal"] is None, e)

# ----------------------------------------- 6. no default promo, no score in it
SRC = open(os.path.join(_ROOT, "parlay_boost.py")).read()
ok("the module imports nothing: no score, signal, ledger or config reaches a price",
   not [n for n in ast.walk(ast.parse(SRC)) if isinstance(n, (ast.Import, ast.ImportFrom))])
ok("  and never reads a Signal Score", '"score"' not in SRC and "['score']" not in SRC)
for name in ("signal_core.py", "nfl_signals.py", "cfb_signals.py", "epl_signals.py", "betting_signals.py"):
    ok("  {} does not import the pricing module (invariant 1)".format(name),
       "parlay_boost" not in open(os.path.join(_ROOT, name)).read())
JS = open(os.path.join(_ROOT, "web", "insights", "bets.js")).read()
ok("the page starts with every promo field blank",
   'var promo = { boost: "", legs: "", total: "", floor: "" };' in JS)

# ------------------------------------------------- 7. which ticket (suggest)
# The fixture board's seven favourites, and APP +440, which the owner's rule
# keeps out (no leg past +100):
#   TOR -136  CAR -148  CWS -240  BUF -340  BAY -380  KC -575  TULN -1050
ALL = legs(*ROWS)


def names(ticket):
    return sorted(l["label"] for l in ticket["legs"]) if ticket else None


class _Missing(dict):
    """A ticket that was not suggested: every figure reads as NaN, so a check
    on it FAILS instead of crashing the suite on None["ev_boosted"]."""
    def __missing__(self, key):
        return [] if key == "legs" else float("nan")


def got(result, which):
    return result.get(which) or _Missing()


P3 = {"boost_pct": 50, "min_legs": 3, "min_total_american": 300}
s = pb.suggest(ALL, P3)
ok("suggest: 50% boost, 3+ legs, +300 -- the three LONGEST favourites, TOR CAR CWS",
   names(s["value"]) == ["CAR", "CWS", "TOR"], names(s["value"]))
ok("  priced exactly as evaluate() prices those legs: +468, 21.3%, +20.9%",
   got(s, "value")["boosted_american"] == 468 and near(got(s, "value")["win_prob"], 0.2130, 5e-4)
   and near(got(s, "value")["ev_boosted"], pb.evaluate(legs("TOR", "CAR", "CWS"), P3)["ev_boosted"], 1e-12),
   s["value"])
ok("  the underdog is not a leg: 7 eligible of 8, APP +440 left out",
   s["eligible"] == 7 and "APP" in ROWS and all("APP" not in (names(t) or []) for t in (s["value"], s["likeliest"])), s["eligible"])
ok("  every ticket counted meets the terms: 26 qualify, and no three shorter favourites do",
   s["qualifying"] == 26 and pb.check(legs("BUF", "BAY", "KC"), P3) != [], s["qualifying"])
ok("  here the likeliest ticket above the bar is the same ticket",
   names(s["likeliest"]) == names(s["value"]))
brute = []
import itertools  # noqa: E402
pool = [l for l in ALL if l["decimal"] <= 2.0]
for n in (3, 4, 5):
    for combo in itertools.combinations(pool, n):
        r = pb.evaluate(list(combo), P3)
        if r["qualifies"]:
            brute.append((r["ev_boosted"], r["win_prob"], sorted(l["label"] for l in combo)))
ok("  an independent brute force over evaluate() agrees on the count and the best",
   len(brute) == 26 and max(brute)[2] == names(s["value"]), (len(brute), max(brute)))

P2 = {"boost_pct": 50, "min_legs": 2}
s2 = pb.suggest(ALL, P2)
ok("two legs allowed: best value is the two longest, TOR CAR (+21.7%, 31.5%)",
   names(s2["value"]) == ["CAR", "TOR"] and near(got(s2, "value")["ev_boosted"], 0.2174, 5e-4)
   and near(got(s2, "value")["win_prob"], 0.3152, 5e-4), s2["value"])
ok("  FEWER legs beat more: no three-leg ticket is worth as much",
   got(s2, "value")["ev_boosted"] > got(s, "value")["ev_boosted"])
ok("  the likelier ticket is CWS BAY: cashes 51.3%, still worth +12.1%",
   names(s2["likeliest"]) == ["BAY", "CWS"] and near(got(s2, "likeliest")["win_prob"], 0.5134, 5e-4)
   and near(got(s2, "likeliest")["ev_boosted"], 0.1215, 5e-4), s2["likeliest"])
ok("  it keeps at least half the best value, and nothing likelier does",
   got(s2, "likeliest")["ev_boosted"] >= 0.5 * got(s2, "value")["ev_boosted"]
   and not [1 for a, b in itertools.combinations(pool, 2)
            if a["fair"] * b["fair"] > got(s2, "likeliest")["win_prob"] + 1e-9
            and pb.evaluate([a, b], P2)["ev_boosted"] >= 0.5 * got(s2, "value")["ev_boosted"]])
kc = pb.evaluate(legs("KC", "TULN"), P2)
ok("  KC TULN cashes 71.6% and is worth +2.3%: positive, and NOT suggested",
   near(kc["win_prob"], 0.716, 2e-3) and 0 < kc["ev_boosted"] < 0.03
   and names(s2["likeliest"]) != ["KC", "TULN"], kc)

sf = pb.suggest(ALL, dict(P3, leg_floor_american=-200))
ok("a -200 leg floor leaves TOR and CAR only: fewer than 3 legs, said in words",
   sf["eligible"] == 2 and sf["value"] is None and "fewer than 3" in (sf["why"] or ""), sf)
sx = pb.suggest(ALL, dict(P3, min_total_american=1000))
ok("terms no ticket can meet: nothing suggested, and it says so",
   sx["qualifying"] == 0 and sx["value"] is None and sx["likeliest"] is None
   and "no combination" in (sx["why"] or ""), sx)
sl = pb.suggest(ALL, {"boost_pct": 5, "min_legs": 3})
ok("a 5% boost cannot beat the vig: the closest ticket is named, NO ticket is recommended",
   sl["value"] is not None and got(sl, "value")["ev_boosted"] < 0 and sl["likeliest"] is None
   and "not worth" not in (sl["why"] or "") and "worth its stake" in (sl["why"] or ""), sl)
for label, promo in (("no boost", {"min_legs": 3}), ("a zero boost", {"boost_pct": 0, "min_legs": 3})):
    sn = pb.suggest(ALL, promo)
    ok("{}: no suggestion at all (unboosted, every ticket loses the vig)".format(label),
       sn["value"] is None and sn["likeliest"] is None and sn["why"] == "no boost entered", sn)
two = legs("TOR", "CAR", "CWS") + [dict(legs("CWS")[0], decimal=1.9, fair=0.5, label="CWS-other")]
sg = pb.suggest(two, P2)
ok("two legs from one game are never on one ticket",
   sg["eligible"] == 3 and all("CWS-other" not in (names(t) or []) for t in (sg["value"], sg["likeliest"])), sg)
nofair = legs("TOR", "CAR") + [dict(legs("CWS")[0], fair=None)]
ok("a leg with no market chance is not eligible",
   pb.suggest(nofair, P2)["eligible"] == 2)
ok("an even-money leg is eligible and a +101 one is not (the owner's +100 line)",
   len(pb.eligible_legs([priced(100), priced(101), priced(-110)])) == 2)
ok("a wider leg cap admits the underdog, and the arithmetic then reaches for it",
   "APP" in (names(pb.suggest(ALL, P3, leg_max_american=500)["value"]) or []))
many = [priced(-110 - i, game="g%d" % i) for i in range(60)]
sm = pb.suggest(many, {"boost_pct": 50, "min_legs": 4})
ok("a search too large for a phone is skipped by size and reported, not run",
   sm["skipped_sizes"] == [5, 6] and sm["value"] is not None and len(got(sm, "value")["legs"]) == 4, sm["skipped_sizes"])
ok("the page's copy carries the same four constants",
   "var LEG_MAX_DEC = 2.0;" in JS and "var EXTRA_LEGS = 2;" in JS and "var MAX_COMBOS = 1500000;" in JS
   and "var LIKELY_KEEP = 0.5;" in JS and pb.SUGGEST_LEG_MAX_AMERICAN == 100 and pb.SUGGEST_EXTRA_LEGS == 2
   and pb.SUGGEST_MAX_COMBOS == 1500000 and pb.SUGGEST_LIKELY_KEEP == 0.5)

print("parlay boost: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "parlay boost: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for fl in failures:
    print("  FAIL " + fl)
sys.exit(1 if checks["fail"] else 0)
