"""Regression tests for the Bets tab's board (bet_board.py).

Run: python3 -m tools.verify.test_bet_board   (from the repo root)

WHY THIS IS PINNED. The board puts a price, what it needs, and the model's own
record side by side, and each of those can be silently wrong in a way that
reads as a recommendation:

  * THE RECORD MUST BE MATCHED ON PRICE. The first cut matched it on score, and
    on the real 2026-09-25 board that set App State (+440, needs 19%, scored 85)
    beside CFB's 44-14 record for 40+ picks -- a record earned almost entirely
    by favourites -- and called it a 65-point edge. Pinned: a record earned at
    one price never judges a pick at another.
  * AN UNPROVEN RECORD MUST NOT RANK. Under PROVEN_MIN_N graded picks the gap is
    None and the row sorts by score, so a 2-0 start cannot top the list.
  * THE SPLIT is the owner's -200 line: -200 itself is a parlay piece.
  * THE BUILD MUST SURVIVE THE BOARD. It is display-only, so an exception in it
    costs the tab and nothing else.

Sabotage-checked when written, counts measured (PYTHONDONTWRITEBYTECODE=1):
matching the record on score instead of price fails 6 of 28; dropping the
PROVEN_MIN_N gate fails 2; putting -200 on the straight side fails 1; letting
postseason rows count fails 1; re-raising from the board's except in
generate_insights fails 1.

The fixture (bets_fixture.json) is REAL: 14 games from the committed store at
f99baf7 and ledger rows from the same commit, trimmed to the fields read. Its
two edits are listed in its own `_edits` field; the -200 boundary test below
edits one price and says so.
"""

import copy
import json
import os
import sys

import yaml

import bet_board
import generate_insights as gi
from tools.verify import test_game_isolation as iso

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + str(detail) + ")") if detail != "" else ""))


def _fx():
    return json.load(open(os.path.join(HERE, "bets_fixture.json")))


def _config():
    return yaml.safe_load(open(os.path.join(REPO, "config.yaml")))


def _priced_cfb_rows(fx, n=30):
    """The fixture's real CFB rows with the documented break_even edit."""
    return [dict(r, break_even=0.75) for r in fx["cfb_rows"][:n]]


def _legs(board):
    return {l["side"]: l for l in board["straight"] + board["parlay"]}


def test_the_split_and_what_is_left_out():
    fx = _fx()
    board = bet_board.build(fx["entities"], _config(), fx["ledger_real_priced"])
    straight = {l["side"] for l in board["straight"]}
    parlay = {l["side"] for l in board["parlay"]}
    check("parlay pieces are -200 or shorter: BUF -340, KC -575, CWS -240, BAY -380, TULN -1050",
          {"BUF", "KC", "CWS", "BAY", "TULN"} <= parlay, sorted(parlay))
    check("straight bets are longer than -200: CAR +124, TOR -136, APP +440",
          {"CAR", "TOR", "APP"} <= straight, sorted(straight))
    check("a board with no lean contributes nothing (BAL/DAL, NYM/WSH, MOST/SMU)",
          not ({"BAL", "DAL", "NYM", "WSH", "MOST", "SMU"} & (straight | parlay)))
    check("a lean below its sport's standout bar is left out (MIL scored 15, MLB bar 17)",
          "MIL" not in straight | parlay)
    check("a lean the book pulled (MIA, moneyline OFF) is counted as unpriced, not dropped silently",
          board["unpriced"] == 1 and "MIA" not in straight | parlay, board["unpriced"])
    tuln = _legs(board)["TULN"]
    check("a price past the football -1000 cap is still a parlay piece, flagged",
          tuln["past_cap"] is True and tuln["american"] == -1050, tuln)
    check("  and a price inside it is not flagged", _legs(board)["BUF"]["past_cap"] is False)
    buf = _legs(board)["BUF"]
    check("BUF: price, break-even and decimal odds from espn_odds",
          buf["display"] == "-340" and buf["break_even_display"] == "77%"
          and abs(buf["decimal"] - (1 + 100 / 340.0)) < 1e-6, buf)
    check("  the opponent and venue are named for the picked side",
          buf["opponent"] == "LAC" and buf["at_home"] is True, buf)


def test_minus_200_is_a_parlay_piece():
    fx = _fx()
    ents = copy.deepcopy(fx["entities"])
    # EDIT (one field): TOR's real price was -136. Moved to the owner's line.
    ents["822760"]["odds"]["home_ml"] = -200
    board = bet_board.build(ents, _config(), [])
    check("-200 exactly is a parlay piece", "TOR" in {l["side"] for l in board["parlay"]})
    ents["822760"]["odds"]["home_ml"] = -199
    board = bet_board.build(ents, _config(), [])
    check("-199 is a straight bet", "TOR" in {l["side"] for l in board["straight"]})


def test_the_record_is_matched_on_price_not_score():
    fx = _fx()
    # The real CFB rows, UNEDITED: none of them was priced.
    board = bet_board.build(fx["entities"], _config(), fx["cfb_rows"])
    app = _legs(board)["APP"]
    check("THE MISLEADING CASE: a +440 underdog is not judged by a record favourites earned",
          app["record"]["n"] == 0 and app["gap"] is None, app["record"])
    check("  and neither is any other CFB leg, since no CFB pick was ever priced",
          all(l["record"]["n"] == 0 for l in board["straight"] + board["parlay"] if l["sport"] == "cfb"))
    # The one real priced row: NFL, needed 71%, MISS.
    board = bet_board.build(fx["entities"], _config(), fx["ledger_real_priced"])
    buf, kc = _legs(board)["BUF"], _legs(board)["KC"]
    check("the real priced NFL MISS (needed 71%) counts for BUF, which needs 77%: same 70-80% band",
          buf["record"]["n"] == 1 and buf["record"]["hits"] == 0 and buf["record"]["band"] == "70-80%",
          buf["record"])
    check("  and not for KC, which needs 85%", kc["record"]["n"] == 0, kc["record"])
    check("  and not for another sport's leg", _legs(board)["TULN"]["record"]["n"] == 0)


def test_a_proven_band_ranks_and_an_unproven_one_does_not():
    fx = _fx()
    rows = _priced_cfb_rows(fx)
    hits = sum(r["verdict"] == "HIT" for r in rows)
    board = bet_board.build(fx["entities"], _config(), rows)
    bay = _legs(board)["BAY"]
    check("30 priced picks in BAY's band make its record proven",
          bay["record"]["proven"] and bay["record"]["n"] == 30 and bay["record"]["hits"] == hits,
          bay["record"])
    check("  and give it a gap: record minus what the price needs",
          bay["gap"] is not None and abs(bay["gap"] - (hits / 30.0 - bay["break_even"])) < 1e-3, bay["gap"])
    check("a proven leg ranks ahead of higher-scored unproven ones",
          board["parlay"][0]["side"] == "BAY", [l["side"] for l in board["parlay"]])
    board = bet_board.build(fx["entities"], _config(), rows[:29])
    bay = _legs(board)["BAY"]
    check("29 picks are unproven: no gap", bay["record"]["proven"] is False and bay["gap"] is None, bay)
    check("  and the list falls back to score order", board["parlay"][0]["side"] == "KC",
          [l["side"] for l in board["parlay"]])


def test_only_the_standing_record_counts():
    fx = _fx()
    rows = _priced_cfb_rows(fx)
    base = _legs(bet_board.build(fx["entities"], _config(), rows))["BAY"]["record"]["n"]
    post = dict(rows[0], season_phase="postseason", date="2027-01-01", run_id="2027-01-02T10:00:00Z")
    check("a postseason row is left out of the regular-season record",
          _legs(bet_board.build(fx["entities"], _config(), rows + [post]))["BAY"]["record"]["n"] == base)
    est = dict(rows[0], basis="estimate", date="2027-01-03", run_id="2027-01-04T10:00:00Z")
    check("an estimate-graded row is left out",
          _legs(bet_board.build(fx["entities"], _config(), rows + [est]))["BAY"]["record"]["n"] == base)
    push = dict(rows[0], verdict="PUSH", date="2027-01-05", run_id="2027-01-06T10:00:00Z")
    check("a push is left out",
          _legs(bet_board.build(fx["entities"], _config(), rows + [push]))["BAY"]["record"]["n"] == base)
    # A superseded run for a date already on file: the ledger's own latest-run
    # rule keeps the newer run, so this older one must not add a pick.
    old = dict(rows[0], run_id="2000-01-01T00:00:00Z")
    check("a superseded run is not double-counted",
          _legs(bet_board.build(fx["entities"], _config(), rows + [old]))["BAY"]["record"]["n"] == base)


def test_the_pipeline_carries_the_board_and_survives_it_failing():
    r = iso._scenario({"mlb": "ok"}, seed=("mlb",))
    bets = (r["data"].get("insights") or {}).get("bets")
    check("run() attaches a bets board beside the games", isinstance(bets, dict)
          and "straight" in bets and "parlay" in bets, type(bets).__name__)
    saved = bet_board.build
    bet_board.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("forced board outage"))
    try:
        r = iso._scenario({"mlb": "ok"}, seed=("mlb",))
    finally:
        bet_board.build = saved
    games = (r["data"].get("insights") or {}).get("games") or []
    check("a board that raises does not take the build down", r["raised"] is None and games,
          repr(r["raised"]))
    check("  it only costs the tab", "bets" not in (r["data"].get("insights") or {}))


def main():
    for fn in (test_the_split_and_what_is_left_out,
               test_minus_200_is_a_parlay_piece,
               test_the_record_is_matched_on_price_not_score,
               test_a_proven_band_ranks_and_an_unproven_one_does_not,
               test_only_the_standing_record_counts,
               test_the_pipeline_carries_the_board_and_survives_it_failing):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("bet board: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
