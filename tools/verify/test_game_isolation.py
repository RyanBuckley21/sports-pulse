"""Regression tests for per-sport isolation in generate_insights.

Run: python3 -m tools.verify.test_game_isolation   (from the repo root)

WHY THIS IS PINNED. _build_game_entities used to raise whenever any sport's
builder failed, and the comment explaining why is the important part: a failed
build and a genuine off day produce byte-identical output -- an empty slate --
so run() reads the failure as "no games today" and clears that sport's
committed partition, which is the pre-game snapshot signal_report.py grades
against. Raising was how that was prevented.

Containing the failure per sport removes the raise, so the protection now has
to be enforced directly: a FAILED sport's partition is frozen, a genuine OFF
DAY's is still cleared. Those two paths differ by one `if` and produce
identical-looking logs, and getting them backwards would silently destroy
graded history rather than throwing anything -- nobody would notice until a
grading run came up empty. So both directions are measured here, together,
rather than argued.

NO NETWORK AND NO SYNTHETIC ENTITIES. The succeeding builders return the real
committed entities from data/insights.games.json, and every store is redirected
to a temp copy of the real file, so a run of this test cannot touch anything
committed.
"""

import copy
import datetime
import json
import os
import shutil
import sys
import tempfile

import yaml

import generate_insights as gi

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _repo_path(*parts):
    return os.path.join(REPO, *parts)


failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name + (("  (" + detail + ")") if detail else ""))


def _make_builder(behaviour, sport, entities):
    """A stand-in for one sport's entry in GAME_BUILDERS.

    Patched into the REGISTRY, never over the fetcher module's own function:
    GAME_BUILDERS binds the function object at import time, so patching
    fetchers.mlb.build_game_entities would leave the registry pointing at the
    original and the test would silently exercise nothing.
    """
    def builder(config, game_date, box_cache, team_entities=None):
        if behaviour == "fail":
            raise RuntimeError("forced %s outage" % sport)
        if behaviour == "offday":
            return {}, dict(box_cache or {}), []
        return ({k: dict(v, sport=sport) for k, v in entities.items()},
                dict(box_cache or {}), [])
    return builder


def _scenario(behaviours, opt_in=True, seed=("mlb", "nfl")):
    """Run one configuration and report what it did to the stores.

    `behaviours` maps sport -> 'ok' | 'fail' | 'offday'. `opt_in` False calls
    _build_game_entities directly with no failed_sports list, which is
    implied_total.py's contract.
    """
    config = yaml.safe_load(open(_repo_path("config.yaml")))
    real_games = json.load(open(_repo_path("data", "insights.games.json")))
    real_box = json.load(open(_repo_path("data", "boxscores.json")))
    entities = {k: dict(v, gamePk=k) for k, v in real_games["mlb"].items()}

    tmp = tempfile.mkdtemp()
    games_p = os.path.join(tmp, "games.json")
    box_p = os.path.join(tmp, "box.json")
    store_p = os.path.join(tmp, "players.json")
    json.dump({s: copy.deepcopy(real_games["mlb"]) for s in seed}, open(games_p, "w"))
    json.dump({s: copy.deepcopy(real_box["mlb"]) for s in seed}, open(box_p, "w"))
    shutil.copy(_repo_path("data", "insights.json"), store_p)
    before_games = json.load(open(games_p))
    before_box = json.load(open(box_p))

    saved = (gi.GAMES_STORE_PATH, gi.BOXSCORE_CACHE_PATH, gi.GAME_BUILDERS,
             gi._active_game_sports, gi._resolve_training_outcomes,
             gi.training_capture.capture_features)
    gi.GAMES_STORE_PATH, gi.BOXSCORE_CACHE_PATH = games_p, box_p
    gi.GAME_BUILDERS = {s: _make_builder(b, s, entities) for s, b in behaviours.items()}
    gi._active_game_sports = lambda cfg: list(behaviours)
    # Both make network calls and neither is under test here.
    gi._resolve_training_outcomes = lambda *a, **k: None
    gi.training_capture.capture_features = lambda rows: (0, 0)

    data = {"sports": {}}
    raised = None
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        if opt_in:
            gi.run(data, now, config=config, store_path=store_p)
        else:
            gi._build_game_entities(config, now)
    except Exception as e:  # noqa: BLE001 -- the outcome under test
        raised = e
    finally:
        (gi.GAMES_STORE_PATH, gi.BOXSCORE_CACHE_PATH, gi.GAME_BUILDERS,
         gi._active_game_sports, gi._resolve_training_outcomes,
         gi.training_capture.capture_features) = saved

    return {
        "raised": raised, "data": data,
        "games": json.load(open(games_p)), "box": json.load(open(box_p)),
        "before_games": before_games, "before_box": before_box,
        "entities": entities, "tmp": tmp,
    }


def test_one_sport_fails_the_other_builds():
    r = _scenario({"mlb": "ok", "nfl": "fail"})
    check("A: run() returns rather than dying with the failed sport", r["raised"] is None,
          repr(r["raised"]))
    games = (r["data"].get("insights") or {}).get("games") or []
    check("A: data.json still carries the healthy sport's full slate",
          len(games) == len(r["entities"]), "%d games" % len(games))
    check("A: and carries nothing from the failed sport",
          all(g.get("sport") == "mlb" for g in games))
    check("A: THE FAILED SPORT'S PARTITION IS FROZEN, byte for byte",
          r["games"].get("nfl") == r["before_games"].get("nfl"))
    check("A: the healthy sport's partition was rewritten",
          set(r["games"].get("mlb") or {}) == set(r["entities"]))
    check("A: the failed sport's boxscore cache is carried through, not replaced",
          r["box"].get("nfl") == r["before_box"].get("nfl"))


def test_a_genuine_off_day_still_clears():
    r = _scenario({"mlb": "ok", "nfl": "offday"})
    check("B: run() returns", r["raised"] is None, repr(r["raised"]))
    check("B: AN OFF DAY'S PARTITION IS STILL CLEARED -- unchanged behaviour",
          r["games"].get("nfl") == {}, repr(r["games"].get("nfl"))[:60])
    # Without this the check above would pass trivially on an empty seed, and
    # the freeze/clear pair would prove nothing.
    check("B: the seed really did have something to clear",
          bool(r["before_games"].get("nfl")))


def test_total_failure_is_still_fatal():
    r = _scenario({"mlb": "fail", "nfl": "fail"})
    check("C: every sport failing raises rather than returning an empty slate",
          isinstance(r["raised"], RuntimeError), type(r["raised"]).__name__)
    check("C: and nothing was written to the games store",
          r["games"] == r["before_games"])


def test_single_sport_config_is_unchanged():
    # Production's shape today. One sport failing IS total failure, so this must
    # behave exactly as it did before isolation existed.
    r = _scenario({"mlb": "fail"}, seed=("mlb",))
    check("D: a lone sport failing is still fatal", isinstance(r["raised"], RuntimeError),
          type(r["raised"]).__name__)
    check("D: games store untouched", r["games"] == r["before_games"])


def test_caller_without_the_list_keeps_the_old_contract():
    # implied_total.py unpacks three values and passes no failed_sports. It
    # cannot freeze anything, so it must keep failing loudly on ANY failure.
    r = _scenario({"mlb": "ok", "nfl": "fail"}, opt_in=False)
    check("E: any failure re-raises for a caller that did not opt in",
          r["raised"] is not None, "returned normally")
    check("E: and it is the builder's own error, not the total-failure guard",
          isinstance(r["raised"], RuntimeError) and "forced" in str(r["raised"]),
          str(r["raised"])[:80])


def main():
    for fn in (test_one_sport_fails_the_other_builds,
               test_a_genuine_off_day_still_clears,
               test_total_failure_is_still_fatal,
               test_single_sport_config_is_unchanged,
               test_caller_without_the_list_keeps_the_old_contract):
        fn()
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("game isolation: all %d checks pass" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
