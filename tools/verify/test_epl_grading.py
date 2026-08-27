"""Regression tests for EPL grading in signal_report.

Run: python3 -m tools.verify.test_epl_grading   (from the repo root)

WHY THIS IS PINNED. A draw grades OPPOSITE ways in the two EPL markets -- it
WINS a double_chance pick and LOSES a match_result one -- and getting that
backwards throws no error. It writes a confident wrong verdict into an
append-only ledger, and because draws are 23.6% of matches it would do so on
roughly one pick in four, making a winning market read as a losing system. There
is no exception to notice and no test that fails on its own; the only way to
catch it is to assert both directions on real drawn matches.

The fixture is REAL ESPN data, captured from live responses over four 2025/26
matchdays and trimmed to the fields the adapter reads: 21 completed matches, 7
home wins, 8 draws, 6 away wins. Real rather than hand-written because a
hand-written draw is a guess about the shape ESPN emits for one, and the shape
is what the adapter parses. Offline so the suite is deterministic.

Also asserts the adapter boundary itself: MLB's registry entries must still be
MLB's own functions, since the whole point of the refactor was that no EPL
change can reach an MLB verdict.
"""

import json
import os
import sys

import epl_grading
import signal_report

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epl_matches_fixture.json")

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + detail + ")") if detail else ""))


def _matches():
    return json.load(open(FIXTURE))


def _pick(event, bet_type, side_is_home, with_draw):
    """A pick on this match, on whichever side the caller asks for."""
    sides = epl_grading._sides(event)
    abbr = sides["home"]["abbr"] if side_is_home else sides["away"]["abbr"]
    return {"gamePk": str(event["id"]), "bet_type": bet_type,
            "side": ("%s or Draw" % abbr) if with_draw else abbr,
            "away_abbr": sides["away"]["abbr"], "home_abbr": sides["home"]["abbr"],
            "score": 80, "market": bet_type, "flags": []}


def test_a_draw_wins_double_chance_and_loses_match_result():
    drawn = [e for e in _matches().values()
             if epl_grading.is_final(e) and epl_grading.observed_facts(e)["result"] == "D"]
    check("fixture actually contains drawn matches", len(drawn) >= 5, "%d" % len(drawn))
    for event in drawn:
        for side_is_home in (True, False):
            dc = epl_grading.grade(_pick(event, "double_chance", side_is_home, True), event, False)
            mr = epl_grading.grade(_pick(event, "match_result", side_is_home, False), event, False)
            check("DRAW + double_chance is HIT", dc[1] == "HIT", "%s got %s" % (event["id"], dc[1]))
            check("DRAW + match_result is MISS", mr[1] == "MISS", "%s got %s" % (event["id"], mr[1]))
            # Not a push. 1X2 has no push and neither market can produce one;
            # a PUSH here would quietly remove the pick from the record instead
            # of counting it.
            check("a draw never grades PUSH", "PUSH" not in (dc[1], mr[1]))


def test_a_decisive_match_grades_by_the_winner():
    for event in _matches().values():
        if not epl_grading.is_final(event):
            continue
        facts = epl_grading.observed_facts(event)
        if facts["result"] == "D":
            continue
        winner_is_home = facts["result"] == "H"
        for bet_type, with_draw in (("match_result", False), ("double_chance", True)):
            on_winner = epl_grading.grade(_pick(event, bet_type, winner_is_home, with_draw), event, False)
            on_loser = epl_grading.grade(_pick(event, bet_type, not winner_is_home, with_draw), event, False)
            check("%s on the winner is HIT" % bet_type, on_winner[1] == "HIT",
                  "%s got %s" % (event["id"], on_winner[1]))
            # Even double_chance loses when the OTHER side wins outright -- the
            # draw is the only extra outcome it covers, not "anything but a
            # loss".
            check("%s on the losing side is MISS" % bet_type, on_loser[1] == "MISS",
                  "%s got %s" % (event["id"], on_loser[1]))


def test_a_graded_row_carries_the_basis_and_the_result():
    event = next(e for e in _matches().values() if epl_grading.is_final(e))
    text, verdict, basis = epl_grading.grade(_pick(event, "match_result", True, False), event, False)
    check("a settled pick is graded on the outcome", basis == "outcome", repr(basis))
    check("the result text names both clubs",
          epl_grading._sides(event)["home"]["abbr"] in text and
          epl_grading._sides(event)["away"]["abbr"] in text, text)
    facts = epl_grading.observed_facts(event)
    check("observed facts carry the three-way result", facts["result"] in ("H", "D", "A"))
    check("observed facts carry both scores",
          facts["home_score"] is not None and facts["away_score"] is not None)


def test_unsettled_and_malformed_inputs_never_grade():
    event = next(e for e in _matches().values() if epl_grading.is_final(e))
    pick = _pick(event, "match_result", True, False)

    missing = epl_grading.grade(pick, None, False)
    check("a match absent from the slate is UNRESOLVED", missing[1] == "UNRESOLVED")

    # A side naming no club in this match must not fall through to a comparison
    # that would score it as a clean MISS.
    bogus = dict(pick, side="ZZZ")
    check("a side naming neither club is UNRESOLVED",
          epl_grading.grade(bogus, event, False)[1] == "UNRESOLVED")

    unknown = dict(pick, bet_type="run_line")
    check("a market with no EPL rule is UNRESOLVED",
          epl_grading.grade(unknown, event, False)[1] == "UNRESOLVED")

    # An unfinished match is PENDING -- re-runnable -- not a loss.
    pre = json.loads(json.dumps(event))
    pre["competitions"][0]["status"]["type"] = {"completed": False, "state": "pre",
                                                "name": "STATUS_SCHEDULED",
                                                "shortDetail": "Sat 3:00 PM"}
    check("an unplayed match is PENDING", epl_grading.grade(pick, pre, False)[1] == "PENDING")
    check("an unplayed match has no observed facts", epl_grading.observed_facts(pre) is None)

    off = json.loads(json.dumps(event))
    off["competitions"][0]["status"]["type"] = {"completed": False, "state": "post",
                                                "name": "STATUS_POSTPONED",
                                                "description": "Postponed"}
    check("a postponed match is POSTPONED", epl_grading.grade(pick, off, False)[1] == "POSTPONED")
    check("is_called_off recognises it", epl_grading.is_called_off(off))


def test_the_adapter_boundary_holds():
    mlb = signal_report.SPORT_ADAPTERS["mlb"]
    check("MLB still grades with its own function", mlb["grade"] is signal_report.grade)
    check("MLB still reads its own observed_facts",
          mlb["observed_facts"] is signal_report.observed_facts)
    check("MLB still uses its own is_final", mlb["is_final"] is signal_report.is_final)
    check("MLB's store is one date", mlb.get("store_spans_dates") is False)
    epl = signal_report.SPORT_ADAPTERS["epl"]
    check("EPL grades with epl_grading", epl["grade"] is epl_grading.grade)
    # The builder looks three days ahead, so off-date fixtures must be deferred
    # rather than graded -- without this flag they record as UNRESOLVED.
    check("EPL's store is flagged as spanning dates", epl.get("store_spans_dates") is True)
    check("an unregistered sport has no adapter", signal_report.adapter_for("nfl") is None)


def main():
    for fn in (test_a_draw_wins_double_chance_and_loses_match_result,
               test_a_decisive_match_grades_by_the_winner,
               test_a_graded_row_carries_the_basis_and_the_result,
               test_unsettled_and_malformed_inputs_never_grade,
               test_the_adapter_boundary_holds):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in sorted(set(failures)):
            print("  " + f)
        return 1
    print("epl grading: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
