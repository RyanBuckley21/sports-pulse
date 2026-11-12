"""The Bets tab: every priced moneyline lean on today's boards, across sports,
split into STRAIGHT BETS and PARLAY PIECES, each beside what its price needs and
what the model's own graded record says picks like it have done.

WHY IT EXISTS. The owner's stated use, 2026-09-25: "quickly find good straight
bets and parlay pieces for bets with low odds, for all the sports I'm
interested in." The Games tab answers that one league and one card at a time;
this answers it in one list.

WHAT IT DOES NOT CLAIM, and the reason every row carries two numbers. A Signal
Score cannot see price, and the one long-run test against real prices says its
confidence tracks the market's: NFL 2015-2020 out-of-sample, 649 picks, 67.3%
right at prices needing 64.7%, ROI -3.05% with a CI spanning zero (config.yaml,
betting_signals.nfl). So a high score is not an edge, and this board never
sorts or labels as though it were. Each leg shows:

  * BREAK-EVEN -- the hit rate its price needs (espn_odds.break_even, vig in).
  * RECORD AT THIS PRICE -- how the model's graded moneyline picks in the
    same sport did when they were priced in the same ten-point break-even band
    (a -340 pick, needing 77%, is judged by the 70-80% picks). Regular season,
    outcome-graded, the ledger's own latest-run rule
    (signal_report.latest_run_rows), pushes out.

THE RECORD IS MATCHED ON PRICE, NOT SCORE, because matching on score was
measured to mislead on the first real board. On 2026-09-25 CFB's moneyline
picks scored 40+ stood at 44-14 (76%) -- nearly all of them favourites, many at
four-figure prices. Set beside a +440 underdog scored 85 (needs 19%), that
record read as a 65-point edge. It is nothing of the kind: it says favourites
win, which the price already said. A hit rate only means something beside the
price it was earned at. Price capture began on 2026-09-24, so these records
start at zero and fill as priced picks are graded.

A record under PROVEN_MIN_N picks is shown but marked unproven and never ranked
on. Until a band is proven, both lists rank by Signal Score, and the page says
that score is not a measure of value.

THE SPLIT is the owner's line, not a measurement: a lean priced at -200 or
shorter (needs 66.7%+) is a PARLAY PIECE, anything longer is a STRAIGHT BET.
Pieces include games the football bettability filter removed as bets (past
-1000): that filter declines them as STRAIGHT bets, and the owner asked for the
short-priced legs specifically. They carry `past_cap` so the page can say so.

BOOSTED PARLAYS (2026-11-10). Each leg also carries `fair`, the market's own
no-vig chance for its side, so the tray can say what a profit boost is worth
on the legs picked. The arithmetic and its evidence live in parlay_boost.py.

THE SINGLES LINE AND THE START (2026-11-12). The board also carries
`singles_min` (-150, the owner's line for a bet placed alone, narrower than the
split) and each row its game's `start_utc`, so the page can count the straight
bets he would actually place and drop a game once it has started.

DISPLAY ONLY. Nothing here reaches a Signal Score, the ledger or grading; it
reads the scores and prices after the fact, like espn_odds' other readers.
"""

import espn_odds
import nfl_winprob
import parlay_boost
import signal_report

# The owner's parlay-piece line (2026-09-25): -200 or shorter.
PARLAY_MAX_AMERICAN = -200
# The owner's SINGLES line (2026-11-10): nothing shorter than -150. Narrower
# than the split above, which stays as he set it; the page counts the straight
# bets that meet this and says what was measured there (no edge: -7.4% on 279
# NFL picks at closing prices, nfl_odds_backtest.py).
SINGLES_MIN_AMERICAN = -150
# Below this many graded picks a record is shown but marked unproven and never
# ranked on. 30 is the usual floor for a proportion's normal interval to mean
# anything; at n=30 and p=0.7 the 95% interval is still about +/-16 points.
PROVEN_MIN_N = 30
SPORT_LABELS = {"mlb": "MLB", "nfl": "NFL", "cfb": "CFB", "epl": "EPL", "nba": "NBA"}


def record_table(ledger_rows):
    """{sport: [(break_even, hit), ...]} -- every graded, PRICED, regular-season
    moneyline pick the standing record currently counts. Unpriced rows (all of
    them before 2026-09-24) cannot be matched to a price and are left out."""
    table = {}
    for rows in signal_report.latest_run_rows(ledger_rows or []).values():
        for r in rows:
            if (r.get("bet_type") != "moneyline" or r.get("basis") != "outcome"
                    or r.get("verdict") not in ("HIT", "MISS")
                    or r.get("break_even") is None
                    or signal_report._row_phase(r) != "regular"):
                continue
            table.setdefault(signal_report._row_sport(r), []).append(
                (float(r["break_even"]), r["verdict"] == "HIT"))
    return table


def record_for(table, sport, break_even):
    """This sport's graded moneyline picks whose price needed a hit rate in the
    same ten-point band as this one: a pick needing 77% is judged by the
    70-80% picks."""
    lo = min(int(break_even * 10), 9) / 10.0
    picks = [hit for be, hit in table.get(sport, []) if lo <= be < lo + 0.1]
    n, hits = len(picks), sum(picks)
    return {
        "band": "{:.0f}-{:.0f}%".format(100 * lo, 100 * lo + 10),
        "hits": hits,
        "n": n,
        "rate": round(hits / n, 4) if n else None,
        "rate_display": "{:.0f}%".format(100.0 * hits / n) if n else None,
        "proven": n >= PROVEN_MIN_N,
    }


def _leg(ent, threshold, table, max_break_even):
    """One moneyline lean as a board row, or None when there is no lean, it is
    below the sport's standout bar, or the feed never priced it."""
    ml = (ent.get("betting_signals") or {}).get("moneyline") or {}
    home = (ent.get("home") or {}).get("abbr")
    away = (ent.get("away") or {}).get("abbr")
    side, score = ml.get("side"), ml.get("score")
    if side not in (home, away) or score is None or score < threshold:
        return None
    american = espn_odds.for_side(ent.get("odds"), side, home, away, bet_type="moneyline")
    be = espn_odds.break_even(american)
    dec = espn_odds.american_to_decimal(american)
    if american is None or be is None or dec is None:
        return None
    sport = ent.get("sport")
    rec = record_for(table, sport, be)
    # The MARKET's chance for this side: both moneylines with the vig removed
    # (parlay_boost.no_vig). None when the other side has no price -- one side
    # alone cannot say how much of its price is margin. This is what the tray
    # prices a boosted parlay from; it is not a model estimate and the Signal
    # Score plays no part in it (see parlay_boost's docstring for why).
    fair = parlay_boost.fair_prob(ent.get("odds"), side == home)
    # The MODEL's chance for the same side, where the sport has a fitted one
    # (NFL: nfl_winprob). Compared with the market's only to raise a caution,
    # never to rank or select: measured walk-forward on 1,829 NFL games, a
    # side the model rates 5+ points above the no-vig price won 2.4 points
    # LESS often than that price said [-4.5, -0.3]. So a lean the market does
    # not share is, if anything, a worse leg -- and the row says so. The
    # evidence is thin (the interval only just excludes zero, and the 10- and
    # 15-point cuts do not), which is why this is a note and not a filter.
    wp = ent.get("win_prob") or {}
    model_prob = wp.get("home") if side == home else wp.get("away")
    notes = list(ent.get("caution_notes") or [])
    over = None
    if model_prob is not None and fair is not None:
        over = model_prob - fair
        # 1e-9: both numbers are floats, and "exactly 5 points" must not
        # depend on which way a subtraction rounded.
        if over >= nfl_winprob.DISAGREE_MIN - 1e-9:
            notes.append(
                "Model {:.0f}% vs market {:.0f}%: leans the market doesn't share "
                "have won less often than priced".format(100 * model_prob, 100 * fair))
    return {
        "sport": sport,
        "sport_label": SPORT_LABELS.get(sport, (sport or "").upper()),
        "id": ent.get("gamePk"),
        "away": away,
        "home": home,
        "start": ent.get("start"),
        # The start as an instant (None from a builder that has none). The
        # page hides a row once this has passed on the reader's own clock: it
        # is static between deploys, and a game that has kicked off is not a
        # bet. See slate_clock.utc_iso.
        "start_utc": ent.get("start_utc"),
        "side": side,
        "opponent": home if side == away else away,
        "at_home": side == home,
        "score": score,
        "american": int(american),
        "display": "{:+d}".format(int(american)),
        # Decimal odds, so the page's parlay tray can multiply legs without a
        # second American-odds conversion of its own.
        "decimal": round(dec, 6),
        "break_even": round(be, 4),
        "break_even_display": "{:.0f}%".format(100.0 * be),
        "fair": round(fair, 6) if fair is not None else None,
        "model_prob": model_prob,
        "model_over_market": round(over, 4) if over is not None else None,
        "provider": (ent.get("odds") or {}).get("provider"),
        "record": rec,
        # Record at this price minus what the price needs, only where the
        # record is big enough to mean something. None otherwise -- a 2-0
        # start is not a +33-point edge.
        "gap": round(rec["rate"] - be, 4) if rec["proven"] else None,
        "past_cap": bool(max_break_even is not None and be > max_break_even),
        # The builder's cautions (an NFL starting QB on the injury report, a
        # CFB lean on three or fewer games), shown on the row because a leg is
        # exactly where they matter -- see generate_insights' caution_notes.
        "notes": notes,
    }


def build(entities, config, ledger_rows):
    """{"straight": [...], "parlay": [...], "unpriced": n, "proven_min_n": n}.

    Both lists rank proven gaps first (largest first), then by Signal Score,
    then -- for parlay pieces -- by the cheaper break-even. Both break ties on the
    matchup so the order is stable between runs."""
    bs_cfg = (config or {}).get("betting_signals") or {}
    table = record_table(ledger_rows)
    straight, parlay, unpriced = [], [], 0
    for ent in (entities or {}).values():
        sport = ent.get("sport")
        cfg = bs_cfg.get(sport) or {}
        threshold = cfg.get("standout_threshold", 0)
        leg = _leg(ent, threshold, table, cfg.get("max_break_even"))
        if leg is None:
            ml = (ent.get("betting_signals") or {}).get("moneyline") or {}
            sides = ((ent.get("home") or {}).get("abbr"), (ent.get("away") or {}).get("abbr"))
            if ml.get("side") in sides and (ml.get("score") or 0) >= threshold:
                unpriced += 1
            continue
        (parlay if leg["american"] <= PARLAY_MAX_AMERICAN else straight).append(leg)

    def matchup(leg):
        return "{}@{}".format(leg["away"], leg["home"])

    straight.sort(key=lambda l: (l["gap"] is None, -(l["gap"] or 0), -l["score"], matchup(l)))
    parlay.sort(key=lambda l: (l["gap"] is None, -(l["gap"] or 0), -l["score"],
                               l["break_even"], matchup(l)))
    return {"straight": straight, "parlay": parlay, "unpriced": unpriced,
            "parlay_max": PARLAY_MAX_AMERICAN, "singles_min": SINGLES_MIN_AMERICAN,
            "proven_min_n": PROVEN_MIN_N}
