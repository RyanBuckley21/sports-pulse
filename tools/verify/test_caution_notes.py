"""Regression tests for the card's caution notes (display only): the NFL
starting-QB injury note and, since 2026-09-26, the CFB early-read note.

Run: python3 -m tools.verify.test_caution_notes   (from the repo root)

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

THE CFB EARLY-READ NOTE (fetchers.cfb.early_form_note) says a lean rests on
three or fewer FBS games per team, unadjusted for opponent -- App State +440 at
NC State, 2026-09-25, two games each. Measured cost of that early read in the
2023-25 CFB backtest: weeks 2-4 hit 66.2% vs 78.0% from week 8 (scores 40-79).
Sabotage: moving the bar from "three or fewer" to "two or fewer" fails 1 of 17.

The fixture (nfl_injuries_fixture.json) is REAL: nflverse's injuries_2026.csv
as published 2026-09-25 12:43Z, five rows trimmed to the fields read, no edits.
"""

import json
import os
import sys

import bet_board
import generate_insights
from fetchers import cfb, nfl

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
           "caution_notes": notes}
    games = generate_insights._build_games_section({"g": ent}, {})
    check("the Games card receives the notes", games[0].get("caution_notes") == notes, games[0].get("caution_notes"))
    board = bet_board.build({"g": ent}, {"betting_signals": {"nfl": {"standout_threshold": 50}}}, [])
    legs = board["straight"] + board["parlay"]
    check("the Bets row carries them beside the price", legs and legs[0]["notes"] == notes, legs)
    # Display only: the scoring module never names the field.
    import nfl_signals
    src = open(nfl_signals.__file__).read()
    check("nothing in nfl_signals reads the notes", "caution_notes" not in src)


def test_the_cfb_early_read_note():
    # The inputs are build_team_form's output shape; the game counts are the
    # real ones for App State @ NC State on 2026-09-25 (two FBS games each).
    two, four = {"games": 2}, {"games": 4}
    n = cfb.early_form_note("APP", "NCSU", two, two, "APP")
    check("CFB: a lean on two games each gets the early-read note",
          n == "Early read: APP 2 FBS games, NCSU 2 -- stats are not adjusted for opponent strength", n)
    check("  three games is still early (the bar is three or fewer)",
          cfb.early_form_note("APP", "NCSU", {"games": 3}, four, "APP") is not None)
    check("  four games each is not", cfb.early_form_note("APP", "NCSU", four, four, "APP") is None)
    check("  no lean, no note", cfb.early_form_note("APP", "NCSU", two, two, "No clear lean") is None)
    check("  a team with no form (a fallback-tier game) gets no PPA note",
          cfb.early_form_note("APP", "NCSU", {}, two, "APP") is None)


def main():
    for fn in (test_the_notes, test_it_reaches_the_card_and_the_bets_row_only,
               test_the_cfb_early_read_note):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("caution notes: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
