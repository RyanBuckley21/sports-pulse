"""Regression tests for the starting-QB availability note (display only).

Run: python3 -m tools.verify.test_availability_notes   (from the repo root)

WHY THIS IS PINNED. The NFL model's QB override (fetchers.nfl.qb_out) acts only
on an official Out or Doubtful. On 2026-09-25 Caleb Williams had missed every
practice with no game status yet (a Monday game's final report comes on
Saturday), CHI was the model's moneyline side at 62, and the market had moved
CHI from -118 to +185. The card said nothing. The note is the fix, and three
things about it fail quietly:

  * A starter who did not practice, with no status yet, MUST produce a note --
    that is the whole case.
  * A clean bill (Full participation, no status) must produce NONE, or every
    card grows a warning and they stop being read.
  * It must stay display only: it rides the entity to the card and the Bets
    row, and nothing in scoring reads it.

Sabotage-checked when written (PYTHONDONTWRITEBYTECODE=1): noting only rows
with a game status (the qb_out bar) fails 3 of 12; noting Full participation
fails 2; dropping the field from generate_insights' games allowlist fails 1;
dropping it from the bets row fails 1.

The fixture (nfl_injuries_fixture.json) is REAL: nflverse's injuries_2026.csv
as published 2026-09-25 12:43Z, five rows trimmed to the fields read, no edits.
"""

import json
import os
import sys

import bet_board
import generate_insights
from fetchers import nfl

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = json.load(open(os.path.join(HERE, "nfl_injuries_fixture.json")))["rows"]
ID = {(r["full_name"], r["week"]): r["gsis_id"] for r in ROWS}

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + str(detail) + ")") if detail != "" else ""))


def note(name, week, team):
    return nfl.qb_availability_note(ROWS, ID[(name, str(week))], name, team, week)


def test_the_notes():
    n = note("Caleb Williams", 3, "CHI")
    check("THE CASE: a starter who did not practice, no status yet, gets a note", n is not None, n)
    check("  naming team, player, practice status, injury and report week",
          n == "CHI QB Caleb Williams: did not practice (hamstring) (week 3 injury report)", n)
    check("  while qb_out, the score's override, still does NOT fire on it",
          nfl.qb_out(ROWS, ID[("Caleb Williams", "3")], 3) is False)
    check("a clean bill (Full participation, no status) is no note at all",
          note("Patrick Mahomes", 3, "KC") is None, note("Patrick Mahomes", 3, "KC"))
    burrow = note("Joe Burrow", 2, "CIN")
    check("an official status is named, and Full practice adds nothing",
          burrow == "CIN QB Joe Burrow: Questionable (week 2 injury report)", burrow)
    murray = note("Kyler Murray", 2, "MIN")
    check("Out and a limited practice both appear",
          murray == "MIN QB Kyler Murray: Out, limited in practice (concussion) (week 2 injury report)", murray)
    oc = note("Aidan O'Connell", 3, "LV")
    check("a non-injury absence reads as what the report says",
          oc is not None and "did not practice (not injury related - personal matter)" in oc, oc)
    check("another week's row is never used",
          nfl.qb_availability_note(ROWS, ID[("Caleb Williams", "3")], "Caleb Williams", "CHI", 4) is None)
    check("no known starter (week 1) is no note",
          nfl.qb_availability_note(ROWS, None, None, "CHI", 3) is None)


def test_it_reaches_the_card_and_the_bets_row_only():
    notes = [note("Caleb Williams", 3, "CHI")]
    ent = {"gamePk": "2026_03_PHI_CHI", "sport": "nfl", "away": {"abbr": "PHI"}, "home": {"abbr": "CHI"},
           "start": "Mon Sep 28 · 8:15 PM ET",
           "betting_signals": {"moneyline": {"side": "CHI", "score": 62, "flags": []}},
           "standout": {"bet_type": "moneyline", "side": "CHI", "score": 62},
           "odds": {"home_ml": 185, "away_ml": -225, "provider": "DraftKings"},
           "availability_notes": notes}
    games = generate_insights._build_games_section({"g": ent}, {})
    check("the Games card receives the notes", games[0].get("availability_notes") == notes, games[0].get("availability_notes"))
    board = bet_board.build({"g": ent}, {"betting_signals": {"nfl": {"standout_threshold": 50}}}, [])
    legs = board["straight"] + board["parlay"]
    check("the Bets row carries them beside the price", legs and legs[0]["notes"] == notes, legs)
    # Display only: the scoring module never names the field.
    import nfl_signals
    src = open(nfl_signals.__file__).read()
    check("nothing in nfl_signals reads the notes", "availability_notes" not in src)


def main():
    for fn in (test_the_notes, test_it_reaches_the_card_and_the_bets_row_only):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("availability notes: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
