"""Regression tests for MLB price capture (espn_odds.attach_by_teams) and the
moneyline-only market gate (espn_odds.PRICED_MARKETS).

Run: python3 -m tools.verify.test_mlb_odds   (from the repo root)

WHY THIS IS PINNED. Three things here fail silently, each writing a confident
wrong number into an append-only ledger or onto a card:

  * THE JOIN. MLB's store is keyed by StatsAPI gamePk, which ESPN has never
    heard of, and the two feeds' ABBREVIATIONS DISAGREE for two clubs
    (StatsAPI AZ/CWS vs ESPN ARI/CHW on the fixture slate). A join on
    abbreviations would price 28 teams and never say the other two were
    missing, so the join is on full team names -- all 30 match.
  * DOUBLEHEADERS. One name pair appears twice. StatsAPI listed BAL@NYY game 2
    at a placeholder 20:10Z while ESPN had its real 23:05Z start, so our side is
    ordered by GAME NUMBER, not by start time; and a pair whose game count
    differs between feeds is left unmatched rather than guessed.
  * THE MARKET GATE. for_side/clv price a pick by the leading token of its
    side -- a team abbr for every team-named market. NFL and CFB score only
    moneyline, so that was harmless; MLB scores six, and without the gate a
    team_total "NYM Under", a run_line or a first_five_moneyline pick would be
    stamped with the full-game moneyline. Pinned at all three readers: the
    helpers, the ledger (signal_report.collect_picks) and the card
    (generate_insights._price_block).

Sabotage-checked when written, counts measured: removing the gate from
for_side/clv fails the nine wrong-market assertions -- three markets at each
of helpers, ledger and card (9 of 29); reading ESPN's abbreviation instead of
its name fails every join assertion, AZ/CWS included (10 of 29); dropping the
game-count check fails the two ambiguous-pair assertions (2 of 29); ordering
by start time instead of game number fails exactly the placeholder-time
assertion (1 of 29).

The fixture is REAL: the ESPN MLB scoreboard and the StatsAPI schedule for
2026-09-25, trimmed to the fields read. The one edit is in the placeholder
test, which moves game 2's StatsAPI start ahead of game 1's -- the one field
that ordering reads -- and says so where it does it.
"""

import copy
import json
import os
import sys

import espn_odds
import generate_insights
import signal_report

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mlb_odds_fixture.json")

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append("%s  (%s)" % (name, detail) if detail else name)


def _fx():
    with open(FIXTURE) as f:
        return json.load(f)


def _games(fx):
    return {str(g["gamePk"]): (g["teams"]["away"]["team"]["name"], g["teams"]["home"]["team"]["name"],
                               g.get("gameNumber"), g.get("gameDate"))
            for g in fx["statsapi_games"]}


def _by_abbr(fx, abbr):
    return [g for g in fx["statsapi_games"]
            if abbr in (g["teams"]["away"]["team"]["abbreviation"], g["teams"]["home"]["team"]["abbreviation"])]


# Doubleheader truth, read off the fixture by hand: (gamePk -> ESPN event id).
DOUBLEHEADERS = {"824703": "401817104", "824706": "401817074",   # CHC@BOS g1, g2
                 "823489": "401817088", "823491": "401817073"}   # BAL@NYY g1, g2


def test_the_join_is_on_names_and_survives_doubleheaders():
    fx = _fx()
    ids = espn_odds.match_by_teams(fx["espn_events"], _games(fx))
    check("all 17 games join to an ESPN event", len(ids) == 17, "%d" % len(ids))
    for pk, eid in DOUBLEHEADERS.items():
        check("doubleheader game %s -> %s" % (pk, eid), ids.get(pk) == eid, "got %r" % ids.get(pk))
    for abbr in ("AZ", "CWS"):
        games = _by_abbr(fx, abbr)
        check("%s's game joins despite ESPN spelling it differently" % abbr,
              games and all(str(g["gamePk"]) in ids for g in games))
    # The reason the join is on names, stated as a fact about the feeds.
    espn_abbrs = {c["team"]["abbreviation"] for e in fx["espn_events"]
                  for c in e["competitions"][0]["competitors"]}
    check("the feeds really do disagree: ESPN has ARI/CHW, not AZ/CWS",
          {"ARI", "CHW"} <= espn_abbrs and not ({"AZ", "CWS"} & espn_abbrs))


def test_a_placeholder_start_does_not_swap_a_doubleheader():
    fx = _fx()
    games = _games(fx)
    # EDIT (one field): StatsAPI sometimes carries a placeholder time for game
    # 2. Move it AHEAD of game 1's so start-time ordering would swap the pair.
    away, home, number, _ = games["823491"]
    games["823491"] = (away, home, number, "2026-09-25T19:00:00Z")
    ids = espn_odds.match_by_teams(fx["espn_events"], games)
    check("game 2 still joins game 2's event when its start reads earlier",
          ids.get("823491") == "401817073" and ids.get("823489") == "401817088",
          "g1=%r g2=%r" % (ids.get("823489"), ids.get("823491")))


def test_an_ambiguous_pair_is_left_unpriced():
    fx = _fx()
    events = [e for e in fx["espn_events"] if e["id"] != "401817073"]   # ESPN loses BAL@NYY g2
    ids = espn_odds.match_by_teams(events, _games(fx))
    check("a pair whose game count differs is left unmatched, both games",
          "823489" not in ids and "823491" not in ids, repr({k: ids.get(k) for k in ("823489", "823491")}))
    check("the other 15 still join", len(ids) == 15, "%d" % len(ids))


class _Session:
    def __init__(self, events):
        self.events, self.calls = events, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        events = self.events

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"events": events}
        return R()


def test_attach_prices_by_gamepk_in_one_request():
    fx = _fx()
    entities = {pk: {"sport": "mlb"} for pk in _games(fx)}
    session = _Session(fx["espn_events"])
    hit = espn_odds.attach_by_teams(session, "mlb", entities, ["2026-09-25"], _games(fx))
    priced_events = sum(1 for e in fx["espn_events"] if espn_odds.parse_event(e))
    check("every ESPN-priced game is priced on its gamePk", hit == priced_events == 12,
          "hit %d, priced events %d" % (hit, priced_events))
    check("exactly one request, for the one date", len(session.calls) == 1, "%d" % len(session.calls))
    url, params = session.calls[0]
    check("it is the MLB scoreboard", "/sports/baseball/mlb/scoreboard" in url, url)
    check("single-day dates param, never a range", params.get("dates") == "20260925", repr(params))
    unpriced = [pk for pk, e in entities.items() if "odds" not in e]
    check("an unpriced game carries no odds key", len(unpriced) == 17 - hit)


def _priced_entity(fx):
    """A real priced game, as a store entity with a both-markets betting_signals."""
    ev = next(e for e in fx["espn_events"] if espn_odds.parse_event(e))
    odds = espn_odds.parse_event(ev)
    names = {c["homeAway"]: c["team"]["displayName"] for c in ev["competitions"][0]["competitors"]}
    g = next(g for g in fx["statsapi_games"]
             if g["teams"]["home"]["team"]["name"] == names["home"]
             and g["teams"]["away"]["team"]["name"] == names["away"])
    away, home = g["teams"]["away"]["team"]["abbreviation"], g["teams"]["home"]["team"]["abbreviation"]
    return str(g["gamePk"]), away, home, odds


WRONG_MARKETS = ("team_total", "run_line", "first_five_moneyline")


def test_only_a_moneyline_pick_is_priced_from_the_moneyline():
    pk, away, home, odds = _priced_entity(_fx())
    ml = espn_odds.for_side(odds, home, home, away, bet_type="moneyline")
    check("a moneyline pick gets its side's price", ml == odds["home_ml"] and ml is not None, repr(ml))
    check("no bet_type keeps the old market-blind answer",
          espn_odds.for_side(odds, home, home, away) == odds["home_ml"])
    for bt, side in (("team_total", home + " Under"), ("run_line", home), ("first_five_moneyline", home)):
        check("helpers: %s is not priced from the moneyline" % bt,
              espn_odds.for_side(odds, side, home, away, bet_type=bt) is None
              and espn_odds.clv(odds, side, home, away, bet_type=bt) is None)

    # The ledger: every market for one real priced game, all_markets=True.
    scored = {"moneyline": {"side": home, "score": 40, "flags": []},
              "run_line": {"side": home, "score": 38, "flags": []},
              "first_five_moneyline": {"side": home, "score": 36, "flags": []},
              "team_total": {"home": {"abbr": home, "side": "Under", "score": 34, "flags": []},
                             "away": {"abbr": away, "side": "No clear lean", "score": 0, "flags": []}}}
    store = {pk: {"away": {"abbr": away}, "home": {"abbr": home}, "start": "7:05 PM ET",
                  "betting_signals": scored, "odds": odds}}
    config = signal_report.load_config()
    picks = {p["bet_type"]: p for p in signal_report.collect_picks(store, config, 17, True)}
    check("ledger: the moneyline pick carries its price", picks["moneyline"]["price"] == odds["home_ml"],
          repr(picks["moneyline"]["price"]))
    for bt in WRONG_MARKETS:
        check("ledger: %s carries no price and no CLV" % bt,
              picks[bt]["price"] is None and picks[bt]["clv"] is None,
              "price %r clv %r" % (picks[bt]["price"], picks[bt]["clv"]))

    # The card.
    for bt, side, want_priced in (("moneyline", home, True), ("team_total", home + " Under", False),
                                  ("run_line", home, False), ("first_five_moneyline", home, False)):
        ent = {"odds": odds, "home": {"abbr": home}, "away": {"abbr": away},
               "standout": {"bet_type": bt, "side": side, "score": 40}}
        block = generate_insights._price_block(ent)
        check("card: %s standout %s" % (bt, "shows its price" if want_priced else "shows no price"),
              (block is not None and block["american"] == odds["home_ml"]) if want_priced else block is None,
              repr(block and block.get("american")))


def main():
    for fn in (test_the_join_is_on_names_and_survives_doubleheaders,
               test_a_placeholder_start_does_not_swap_a_doubleheader,
               test_an_ambiguous_pair_is_left_unpriced,
               test_attach_prices_by_gamepk_in_one_request,
               test_only_a_moneyline_pick_is_priced_from_the_moneyline):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in sorted(set(failures)):
            print("  " + f)
        return 1
    print("mlb odds: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
