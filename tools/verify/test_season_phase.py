"""Regression tests for season-phase tagging in the grading ledger.

Run: python3 -m tools.verify.test_season_phase   (from the repo root)

WHY THIS IS PINNED. Nothing in the pipeline filters by game type, so MLB's
playoffs (from 2026-09-30), its spring training, CFB's bowls and NFL's
playoffs are all scored and graded like any other slate. That is deliberate,
but it means the all-time record would quietly absorb a different population
of games -- short series, aces on extra rest, split-squad spring lineups --
and change what its headline number means, with nothing thrown and nothing
on screen to say so. So every pick row names its season phase, and the report
keeps each phase's record apart from the regular season.

Four things are pinned, all silent when wrong:

  * EACH ADAPTER READS ITS OWN FEED'S PHASE. MLB's StatsAPI `gameType`
    (R regular; F/D/L/W/C/P postseason; S preseason; A/I/E exhibition) and
    ESPN's event `season.type` (1 preseason, 2 regular, 3 postseason) for NFL
    and CFB. EPL is always regular -- ESPN's type there is a season ID, not a
    phase. An UNKNOWN code maps to None rather than to "regular", so a new
    code cannot slip into the headline.
  * EVERY PICK ROW IS STAMPED, from the grader's own slate game.
  * A ROW WITH NO TAG READS AS REGULAR. Every row written before the field
    existed is regular season, and the report on those rows must be
    byte-identical to what it printed before tagging existed.
  * THE RECORD KEEPS PHASES APART: once a postseason pick exists, the headline
    is regular season only and the postseason gets its own line.

Sabotage-checked in three directions when written: mapping MLB's wild-card
code "F" to regular fails exactly the MLB-postseason phase and stamping
assertions plus the five split assertions that depend on them (8 of 53);
dropping the phase filter from ledger_totals fails exactly the three
assertions that the headline and the postseason line each hold their own
record (3 of 53); defaulting an untagged row to None instead of "regular"
fails exactly the three legacy-identity assertions (3 of 53). The second of
those also caught a real bug before commit: a 19-character label left no
space before the record ("All-time (regular):Outcome-graded"), which the
headline assertion now pins with the space included.

The fixture is REAL: statsapi.mlb.com and ESPN scoreboard responses captured
2026-09-24 across dates chosen to cover every phase (MLB R/F/D/W/S, NFL
preseason/regular/post-season, CFB regular/bowl/championship, EPL), trimmed
to the fields the adapters read. Offline and deterministic.
"""

import copy
import json
import os
import sys

import signal_report

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "season_phase_fixture.json")

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


# What each captured date IS, per its own league's calendar -- written down
# independently of the code under test, so a wrong mapping cannot agree with
# itself.
EXPECTED = {
    "mlb": {"2026-09-23": "regular", "2025-10-01": "postseason", "2025-10-05": "postseason",
            "2025-10-27": "postseason", "2026-03-01": "preseason"},
    "nfl": {"20260920": "regular", "20260110": "postseason", "20260815": "preseason"},
    "cfb": {"20260919": "regular", "20251220": "postseason", "20260119": "postseason"},
    "epl": {"20260920": "regular"},
}


def test_each_adapter_reads_its_own_feeds_phase():
    fx = _fx()
    for sport, dates in EXPECTED.items():
        fn = signal_report.SPORT_ADAPTERS[sport]["season_phase"]
        for date, want in dates.items():
            games = fx[sport][date]
            check("%s fixture for %s is non-empty" % (sport, date), len(games) > 0)
            for g in games:
                got = fn(g)
                check("%s %s -> %s" % (sport, date, want), got == want, "got %r" % got)
        check("%s: no game -> None" % sport, fn(None) is None)
    # An unknown code must not be promoted into the regular-season record.
    mlb_game = copy.deepcopy(fx["mlb"]["2026-09-23"][0])
    mlb_game["gameType"] = "Z"
    check("mlb: unknown gameType -> None", signal_report.season_phase(mlb_game) is None)
    for sport in ("nfl", "cfb"):
        ev = copy.deepcopy(fx[sport]["20260919" if sport == "cfb" else "20260920"][0])
        ev["season"]["type"] = 99
        check("%s: unknown season.type -> None" % sport,
              signal_report.SPORT_ADAPTERS[sport]["season_phase"](ev) is None)


def _pick(game_pk, away="AAA", home="BBB"):
    return {"gamePk": str(game_pk), "away_abbr": away, "home_abbr": home, "start": "7:05 PM ET",
            "bet_type": "moneyline", "market": "ML", "side": home, "score": 40, "flags": []}


def test_every_pick_row_is_stamped():
    fx = _fx()
    for sport, date, want in (("mlb", "2025-10-01", "postseason"), ("mlb", "2026-09-23", "regular"),
                              ("nfl", "20260110", "postseason"), ("cfb", "20251220", "postseason"),
                              ("epl", "20260920", "regular")):
        game = fx[sport][date][0]
        adapter = signal_report.SPORT_ADAPTERS[sport]
        rows = signal_report.build_pick_rows(
            date, [(_pick(game.get("gamePk") or game.get("id")), game, "r", "HIT", "outcome")],
            "fixture", "run-1", sport_key=sport, sport=adapter)
        check("%s %s row stamped %s" % (sport, date, want),
              rows[0].get("season_phase") == want, "got %r" % rows[0].get("season_phase"))
        check("%s row carries SCHEMA_VERSION %d" % (sport, signal_report.SCHEMA_VERSION),
              rows[0].get("schema_version") == signal_report.SCHEMA_VERSION)
    rows = signal_report.build_pick_rows("2026-09-23", [(_pick(1), None, None, "UNRESOLVED", None)],
                                         "fixture", "run-1")
    check("a pick with no slate game is stamped None, not regular",
          rows[0].get("season_phase") is None)


def _ledger(fx):
    """Two graded dates: a regular-season one (3 HIT, 1 MISS) and a postseason
    one (1 HIT, 2 MISS), each row built by build_pick_rows from a real game."""
    reg_game = fx["mlb"]["2026-09-23"][0]
    post_game = fx["mlb"]["2025-10-01"][0]
    rows = []
    for i, v in enumerate(("HIT", "HIT", "HIT", "MISS")):
        rows += signal_report.build_pick_rows("2026-09-23", [(_pick(1000 + i), reg_game, "r", v, "outcome")],
                                              "fixture", "run-reg")
    for i, v in enumerate(("HIT", "MISS", "MISS")):
        rows += signal_report.build_pick_rows("2026-09-30", [(_pick(2000 + i), post_game, "r", v, "outcome")],
                                              "fixture", "run-post")
    return rows


def test_untagged_rows_read_as_regular():
    fx = _fx()
    check("a row with no season_phase reads as regular", signal_report._row_phase({}) == "regular")
    reg_only = [r for r in _ledger(fx) if r["date"] == "2026-09-23"]
    legacy = []
    for r in reg_only:
        r = dict(r)
        r.pop("season_phase", None)
        legacy.append(r)
    tagged = signal_report.alltime_lines(reg_only)
    untagged = signal_report.alltime_lines(legacy)
    check("tagged-regular and untagged rows report byte-identically", tagged == untagged,
          "%r vs %r" % (tagged, untagged))
    check("an all-regular ledger keeps the plain 'All-time:' headline",
          untagged and untagged[0].startswith("All-time:          "), repr(untagged[:1]))
    check("an all-regular ledger prints no phase line",
          not any(l.startswith(("Postseason:", "Preseason:", "Exhibition:")) for l in untagged))


def test_the_record_keeps_phases_apart():
    lines = signal_report.alltime_lines(_ledger(_fx()))
    head = next((l for l in lines if l.startswith("All-time")), "")
    post = next((l for l in lines if l.startswith("Postseason:")), "")
    check("headline is labelled regular season once a postseason pick exists",
          head.startswith("All-time (regular): "), repr(head))
    check("headline excludes the postseason: 3-1, not the blended 4-3",
          "3-1 (75%) on 4 picks" in head and "4-3" not in head, repr(head))
    check("headline counts only the dates with regular-season picks",
          head.endswith("·  1 date"), repr(head))
    check("the postseason gets its own line with exactly its own record",
          "1-2 (33%) on 3 picks" in post and "1 date" in post, repr(post))
    check("records, counted once each, sum to the whole ledger",
          head.count("on 4 picks") == 1 and post.count("on 3 picks") == 1)


def main():
    for fn in (test_each_adapter_reads_its_own_feeds_phase,
               test_every_pick_row_is_stamped,
               test_untagged_rows_read_as_regular,
               test_the_record_keeps_phases_apart):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in sorted(set(failures)):
            print("  " + f)
        return 1
    print("season phase: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
