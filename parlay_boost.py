"""Boosted-parlay arithmetic: what a sportsbook profit boost is worth on a
given set of legs, priced from the market's own numbers.

WHY THIS EXISTS. The owner bets parlays only when a DraftKings profit boost
makes them worth taking, and the question "does this boost cover the vig on
these legs" is arithmetic with a definite answer. Until 2026-11-10 the Bets
tray answered a different one: it multiplied the legs' prices and, when every
leg's price band had a proven record, the model's hit rates. Nothing said
whether the boosted ticket was worth more than its stake.

THE WIN CHANCE COMES FROM THE MARKET, NOT THE MODEL -- on purpose, and on
evidence. Measured 2026-11-10 over 1,245 NFL picks (2015-2025, closing
prices), the shipped model's win rate minus the no-vig closing probability is
+0.01pp [-2.5, +2.4]: it carries no information the price does not. So a leg's
"fair" chance here is the book's two-sided moneyline with the vig removed, and
the Signal Score is not consulted. This keeps invariant 1 intact from the
other side too: no price reaches a score, and no score reaches a price.

THE NO-VIG METHOD is proportional: p = (1/d_side) / (1/d_home + 1/d_away). It
has no fitted constant, but it is a claim about how the book spreads its
margin, so it was measured (tools/devig_calibration.py, 5,788 NFL sides
2015-2025, realised win rate minus this probability, 4,000-resample CI):

    -500 or shorter   n=251    +2.30pp  [-1.94, +6.14]
    -499..-301        n=526    +0.08pp  [-3.54, +3.55]
    -300..-200        n=666    +0.89pp  [-2.64, +4.27]
    -199..-150        n=712    -1.68pp  [-5.32, +1.96]
    -149..-101        n=828    +0.65pp  [-2.76, +3.91]
    +100..+199        n=1569   +0.56pp  [-1.88, +2.97]
    +200 or longer    n=1234   -1.08pp  [-3.35, +1.24]

  No band is measurably off, and no band is tight: each interval is about
  +/-3.5pp. READ THE EV WITH THAT IN MIND. At a -200 leg (fair ~0.64) 3.5pp is
  about 5% of the leg's chance, and a parlay multiplies it per leg, so a
  four-leg EV printed as +20% is not distinguishable from roughly 0% to +40%
  on this evidence. The measurement is NFL closing lines only; CFB and NBA
  are assumed to behave alike and have not been measured (no price history).

A PROFIT BOOST multiplies the WINNINGS, not the payout: a 50% boost on a +400
ticket pays +600. With P the chance every leg wins and D the parlay's decimal
price, the value of one unit staked is

    EV = P * (1 + (D - 1) * (1 + b)) - 1

  and the boost at which that is exactly zero is b* = (1 - P*D) / (P*D - P).
  P*D is the ticket's return before the boost: a product of legs each paying
  slightly under fair, which is why VIG COMPOUNDS -- four legs each returning
  0.96 return 0.85 together -- and why more legs need a bigger boost.

LEGS ARE TREATED AS INDEPENDENT, which is why two legs from one game are
refused outright rather than priced. Same-game legs are strongly correlated
(P is not the product), and the board offers one lean per game anyway. Legs
from different games are close to independent but not exactly; nothing here
corrects for that, and the only honest check is predicted against realised
hit rate once boosted tickets have been logged -- which nothing does yet
(open item in CLAUDE.md).

THE PROMO IS AN INPUT, NEVER A CONSTANT. DraftKings' terms change by day and
by state -- the owner's description, 2026-11-10: "3+ legs, +300 minimum",
sometimes +400, sometimes a per-leg floor of -200 or -250, sometimes only
+100 on the whole ticket. So every term is optional and supplied by the
caller (the Bets tray's inputs); no default promo exists anywhere.

DISPLAY ONLY. Nothing here reaches a Signal Score, the ledger or grading.
web/insights/bets.js repeats the EV formula for the legs the reader taps;
tools/verify/run.js pins the page's numbers to this module's.
"""

import math


def american_to_decimal(american):
    """American odds -> decimal, or None for zero/garbage."""
    try:
        n = float(american)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return 1.0 + (n / 100.0 if n > 0 else 100.0 / abs(n))


def decimal_to_american(decimal):
    """Decimal -> American, rounded to a whole number as a book prints it."""
    if decimal is None or decimal <= 1.0:
        return None
    return int(round((decimal - 1.0) * 100.0)) if decimal >= 2.0 \
        else int(round(-100.0 / (decimal - 1.0)))


def no_vig(home_american, away_american):
    """(home, away) win probabilities with the vig removed proportionally, or
    None unless BOTH sides carry a usable price -- one side alone cannot say
    how much of its price is margin."""
    home, away = american_to_decimal(home_american), american_to_decimal(away_american)
    if home is None or away is None:
        return None
    inv_home, inv_away = 1.0 / home, 1.0 / away
    total = inv_home + inv_away
    return inv_home / total, inv_away / total


def fair_prob(odds, side_is_home):
    """The no-vig chance of one side from a stored odds block ({"home_ml",
    "away_ml", ...}, espn_odds.parse_event's shape), or None."""
    pair = no_vig((odds or {}).get("home_ml"), (odds or {}).get("away_ml"))
    if pair is None:
        return None
    return pair[0] if side_is_home else pair[1]


def check(legs, promo):
    """Why these legs do NOT qualify for `promo`, as a list of short reasons;
    [] when they do. Every promo term is optional (None = not part of today's
    offer):

      min_legs            fewest legs allowed
      min_total_american  the whole ticket must pay at least this (+300)
      leg_floor_american  no leg may be SHORTER than this (-200 admits -150
                          and +120, refuses -250)
    """
    promo = promo or {}
    reasons = []
    min_legs = promo.get("min_legs")
    if min_legs is not None and len(legs) < min_legs:
        reasons.append("needs {}+ legs".format(int(min_legs)))
    floor = promo.get("leg_floor_american")
    floor_dec = american_to_decimal(floor)
    if floor_dec is not None:
        for leg in legs:
            # Compared in DECIMAL, where "shorter" is simply "smaller": in
            # American terms -250 < -200 < +120 happens to order the same way,
            # but +100 and -100 are one price with two spellings.
            if leg["decimal"] < floor_dec - 1e-9:
                reasons.append("{} is shorter than {:+d}".format(
                    leg.get("label") or "a leg", int(floor)))
    min_total = promo.get("min_total_american")
    min_total_dec = american_to_decimal(min_total)
    if min_total_dec is not None and legs:
        total = 1.0
        for leg in legs:
            total *= leg["decimal"]
        if total < min_total_dec - 1e-9:
            reasons.append("pays under {:+d}".format(int(min_total)))
    return reasons


def evaluate(legs, promo=None):
    """Price a parlay of `legs` under `promo`.

    Each leg is {"game": <id>, "decimal": <price>, "fair": <no-vig chance or
    None>, "label": <optional name>}. Returns

      legs, decimal, american   the unboosted ticket
      qualifies, reasons        against the promo's terms (see check)
      priceable                 False when a chance cannot honestly be stated:
                                a leg with no two-sided price, or two legs
                                from one game. Everything below is then None.
      win_prob                  P, the product of the legs' fair chances
      ev_unboosted              P*D - 1: what the vig alone costs
      break_even_boost          b*, the boost at which the ticket is worth
                                its stake (None if no boost could do it)
      boost, boosted_american   only when promo carries boost_pct
      ev_boosted                the value of one unit staked, boost applied
    """
    promo = promo or {}
    out = {"legs": len(legs), "decimal": None, "american": None,
           "qualifies": None, "reasons": [], "priceable": False, "why": None,
           "win_prob": None, "ev_unboosted": None, "break_even_boost": None,
           "boost": None, "boosted_american": None, "ev_boosted": None}
    if not legs:
        out["why"] = "no legs"
        return out
    decimal = 1.0
    for leg in legs:
        decimal *= leg["decimal"]
    out["decimal"] = decimal
    out["american"] = decimal_to_american(decimal)
    out["reasons"] = check(legs, promo)
    out["qualifies"] = not out["reasons"]

    games = [leg.get("game") for leg in legs]
    if len(set(games)) != len(games):
        out["why"] = "two legs from one game are correlated"
        return out
    if any(leg.get("fair") is None for leg in legs):
        out["why"] = "a leg has no two-sided price"
        return out

    win = 1.0
    for leg in legs:
        win *= leg["fair"]
    gross = win * decimal                   # P*D: the unboosted return per unit
    out["priceable"] = True
    out["win_prob"] = win
    out["ev_unboosted"] = gross - 1.0
    # b* = (1 - P*D) / (P*D - P). The denominator is P*(D-1) > 0 for any real
    # ticket, so b* is negative when the legs are already worth more than the
    # stake and positive when the boost has vig to cover.
    out["break_even_boost"] = (1.0 - gross) / (gross - win) if gross > win else None

    boost_pct = promo.get("boost_pct")
    if boost_pct is not None:
        boost = float(boost_pct) / 100.0
        boosted = 1.0 + (decimal - 1.0) * (1.0 + boost)
        out["boost"] = boost
        out["boosted_american"] = decimal_to_american(boosted)
        out["ev_boosted"] = win * boosted - 1.0
    return out


# ---------------------------------------------------------------------------
# WHICH TICKET (2026-11-12). evaluate() prices legs the reader picked. The
# owner's ask was the other way round: "identify bets each day ... highlight
# parlay pieces", and of a builder he had to drive himself, "doesn't seem too
# useful if I have to do it myself". suggest() takes today's promo and the
# board's legs and names the tickets.
#
# WHAT THE ARITHMETIC SAYS, because it is not what the board's old headings
# implied. With k = P*D (what the vig leaves of a dollar, about 0.96 a leg),
#
#     boosted value = P*(1 + (D-1)*(1+b)) - 1 = k*(1+b) - b*P - 1
#
# so for a given number of legs the value RISES as the win chance P falls: the
# boost multiplies winnings, and a longer ticket has more winnings to
# multiply. Every extra leg costs another slice of vig. The best ticket is
# therefore the FEWEST legs the promo allows at the LONGEST prices it allows --
# not a stack of heavy favourites. Measured on the real board of 2026-11-12
# (29 priced leans, a 50% boost, 3+ legs, +300 minimum):
#
#     best three legs at -105 / -125 / -135     +24.6%, hits 14.4%
#     best ticket made only of -250 or shorter  +12.6%, hits 18.6% (five legs)
#
# and a +300 minimum on three legs needs them to average -170, so three
# "parlay pieces" at -300 do not even qualify.
#
# TWO TICKETS, because "best" depends on what limits the stake:
#   value   the highest boosted value per dollar. The right ticket when the
#           promo caps the stake below what you would otherwise bet, which
#           boosts nearly always do: at a fixed stake, most value per dollar
#           is most dollars.
#   growth  the highest expected growth of a bankroll staked at Kelly,
#               f* = EV / b            (b = the boosted net odds)
#               g  = P*ln(1 + f*b) + (1-P)*ln(1 - f*)
#           This is the standard answer to "which bet is best" once variance
#           counts (Kelly 1956), and the published work on parlays lands in
#           the same place: Whelan, "The Parlay Puzzle" -- even with an edge,
#           an expected-utility bettor should not play long parlays -- and
#           Establish The Run's parlay maths, that bankroll growth matters
#           more than value quoted as a percentage. Growth favours the ticket
#           that cashes more often: on the 2026-11-12 board the value ticket
#           (+24.6%, cashes 14.4%) grows a Kelly bankroll 0.39% a bet and the
#           growth ticket (+21.2%, cashes 22.0%) grows it 0.49%.
#
# The second ticket was first "the likeliest that keeps half the best value"
# (2026-11-12, a judgment constant of mine). Growth replaces it the same day:
# it needs no constant and it is what the literature uses.
#
# Each ticket also carries `kelly`, f* as a share of bankroll. It is an UPPER
# bound, not advice: each leg's chance is only known to a few points, and the
# usual practice is to stake a fraction of Kelly for exactly that reason.
#
# LEGS ARE FAVOURITES ONLY by default (`leg_max_american`, +100): the owner's
# rule for boosted parlays is no leg past +100 -- "a long-odds underdog leg
# makes it a bad bet" (he would rather bet that dog alone). The arithmetic
# above would otherwise always reach for the longest dog on the board.
#
# The chance is the MARKET's. Which side the model leans on does not enter the
# value at all; the board offers its lean for each game because the owner
# decided the tray keeps to the lean (2026-11-10).
# ---------------------------------------------------------------------------
SUGGEST_LEG_MAX_AMERICAN = 100
# Sizes tried: the promo's minimum (2 when it names none) and up to two more.
# Two more is where a floor of -250 can still reach +300; beyond that each leg
# only adds vig.
SUGGEST_EXTRA_LEGS = 2
# Combinations tried per size before a size is skipped. 40 legs choose 5 is
# 658,008; the page repeats this search on a phone.
SUGGEST_MAX_COMBOS = 1500000


def _combos(n, r):
    if r < 0 or r > n:
        return 0
    out = 1
    for i in range(r):
        out = out * (n - i) // (i + 1)
    return out


def eligible_legs(legs, promo=None, leg_max_american=SUGGEST_LEG_MAX_AMERICAN):
    """The legs a suggested ticket may use: a two-sided price (so a chance can
    be stated), no longer than `leg_max_american`, no shorter than the promo's
    leg floor, and one per game (the first listed wins)."""
    promo = promo or {}
    cap = american_to_decimal(leg_max_american)
    floor = american_to_decimal(promo.get("leg_floor_american"))
    out, seen = [], set()
    for leg in legs or []:
        if leg.get("fair") is None or leg.get("decimal") is None:
            continue
        if cap is not None and leg["decimal"] > cap + 1e-9:
            continue
        if floor is not None and leg["decimal"] < floor - 1e-9:
            continue
        if leg.get("game") in seen:
            continue
        seen.add(leg.get("game"))
        out.append(leg)
    return out


def suggest(legs, promo, leg_max_american=SUGGEST_LEG_MAX_AMERICAN):
    """The tickets worth showing for `promo` from `legs` (evaluate()'s leg
    shape, plus a "label").

    Returns {"eligible", "qualifying", "skipped_sizes", "value", "growth",
    "why"}. `value` and `growth` are {"legs": [...], "win_prob", "decimal",
    "boosted_american", "ev_boosted", "kelly", "log_growth"} or None. `growth`
    is None when no qualifying ticket is worth more than its stake (a losing
    bet has no Kelly stake), and may be the same ticket as `value`. `why` says
    so in words when there is nothing to suggest.

    No boost, no suggestion: unboosted, every parlay on the board is worth
    less than its stake by the vig, and ranking losers would read as advice."""
    promo = promo or {}
    out = {"eligible": 0, "qualifying": 0, "skipped_sizes": [],
           "value": None, "growth": None, "why": None}
    boost_pct = promo.get("boost_pct")
    if boost_pct is None or float(boost_pct) <= 0:
        out["why"] = "no boost entered"
        return out
    boost = float(boost_pct) / 100.0
    pool = eligible_legs(legs, promo, leg_max_american)
    # A stable order, so the search and the page's copy of it visit
    # combinations identically: longest price first, then label.
    pool.sort(key=lambda l: (-l["decimal"], str(l.get("label"))))
    out["eligible"] = len(pool)
    min_legs = max(2, int(promo.get("min_legs") or 2))
    min_total = american_to_decimal(promo.get("min_total_american"))
    if len(pool) < min_legs:
        out["why"] = "fewer than {} legs fit these terms".format(min_legs)
        return out

    best_value = best_growth = None

    def kelly(p, dec):
        """(f*, g) for a ticket winning with chance p at boosted decimal odds
        1 + (dec-1)*(1+boost); (0, 0) when it is not worth its stake."""
        net = (dec - 1.0) * (1.0 + boost)
        ev = p * (1.0 + net) - 1.0
        if net <= 0 or ev <= 1e-12:
            return 0.0, 0.0
        f = min(ev / net, 1.0 - 1e-9)
        return f, p * math.log(1.0 + f * net) + (1.0 - p) * math.log(1.0 - f)

    def better_value(a, b):
        return b is None or (a["ev"], a["p"]) > (b["ev"] + 1e-12, b["p"]) or (
            abs(a["ev"] - b["ev"]) <= 1e-12 and a["p"] > b["p"] + 1e-12)

    def better_growth(a, b):
        return b is None or a["g"] > b["g"] + 1e-15 or (
            abs(a["g"] - b["g"]) <= 1e-15 and a["p"] > b["p"] + 1e-12)

    def walk(start, need, dec, p, chosen):
        nonlocal best_value, best_growth
        if need == 0:
            if min_total is not None and dec < min_total - 1e-9:
                return
            out["qualifying"] += 1
            f, g = kelly(p, dec)
            cand = {"ev": p * (1.0 + (dec - 1.0) * (1.0 + boost)) - 1.0, "p": p,
                    "dec": dec, "legs": list(chosen), "f": f, "g": g}
            if better_value(cand, best_value):
                best_value = cand
            if g > 0 and better_growth(cand, best_growth):
                best_growth = cand
            return
        for i in range(start, len(pool) - need + 1):
            leg = pool[i]
            chosen.append(leg)
            walk(i + 1, need - 1, dec * leg["decimal"], p * leg["fair"], chosen)
            chosen.pop()

    sizes = []
    for size in range(min_legs, min_legs + SUGGEST_EXTRA_LEGS + 1):
        if size > len(pool):
            break
        if _combos(len(pool), size) > SUGGEST_MAX_COMBOS:
            out["skipped_sizes"].append(size)
            continue
        sizes.append(size)
    for size in sizes:
        walk(0, size, 1.0, 1.0, [])

    def ticket(c):
        boosted = 1.0 + (c["dec"] - 1.0) * (1.0 + boost)
        return {"legs": c["legs"], "win_prob": c["p"], "decimal": c["dec"],
                "boosted_american": decimal_to_american(boosted), "ev_boosted": c["ev"],
                "kelly": c["f"], "log_growth": c["g"]}

    if best_value is None:
        out["why"] = "no combination of these legs meets the terms"
        return out
    out["value"] = ticket(best_value)
    out["growth"] = ticket(best_growth) if best_growth is not None else None
    if best_growth is None:
        out["why"] = "no qualifying ticket is worth its stake at this boost"
    return out
