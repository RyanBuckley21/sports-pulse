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
