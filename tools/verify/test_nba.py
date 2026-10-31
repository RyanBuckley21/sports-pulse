"""Regression tests for the NBA data layer: which events are games, which day
they are on, and how a pick on one is graded.

Run: python3 -m tools.verify.test_nba   (from the repo root)

WHY THIS IS PINNED. Every rule here fails silently, and into numbers nobody
would think to doubt:

  * THE COUNTED-GAME RULE needs BOTH season.type == 2 AND competition type STD.
    season.type alone admits All-Star games (STARS/WORLD/STRIPES) and the NBA
    Cup final; STD alone admits preseason. Measured on 2025-26 by a session
    with ESPN access: unfiltered, 1,235 games and 33 "teams"; the rule gives
    1,232-1,235 because POSTPONED shells pass it, and only the DECIDED subset
    is 1,230, 30 teams, 82 each. neutralSite separates nothing -- two Cup semifinals and
    three international games are neutral AND count.
  * SLATE DATES ARE EASTERN. The 2025 Mexico City game is stamped
    2025-11-02T02:00Z and was played on November 1; CLE vs OKC, stamped
    2026-11-03T00:00Z, is a November 2 game.
  * FORM READS ONLY DECIDED, COUNTED GAMES. A leaked preseason game, Cup final
    or unplayed 0-0 would each change a team's margin AND its game count --
    the fixture has one of each, on teams that also play a counted game.
  * A LEVEL SCORE IS A BROKEN FEED, not a push: the NBA cannot tie.
  * TEAM_META IS THE CAPTURE. ESPN's codes are GS, NO, NY, SA, UTAH, WSH; a
    table written from memory would miss teams with no error.

Fixtures are REAL and unedited: nba_games_fixture.json (13 events, captured
2026-10-30/31, every case labelled in its own _provenance -- this suite reads
the expected counted set FROM those labels, so the rule is checked against
the capturing session's classification rather than against itself) and
nba_teams_fixture.json (/teams, 30). One edge was never observed and is built
by editing ONE field: a completed game with level scores (both of 401909882's
scores set equal), documented at the assertion. No network; nothing in data/.

Sabotage-checked when written (2026-10-31, PYTHONDONTWRITEBYTECODE=1,
__pycache__ cleared, sources byte-identical after), counts measured, of 73:
  S1 season.type half dropped                 7 fail  (preseason leaks; ATL's
                                                       form gains its IND win)
  S2 competition-type half dropped           15 fail  (All-Star + Cup final leak
                                                       into slates, form, phase)
  S3 neutralSite used as the filter          14 fail  (BOTH ways: real neutral
                                                       games dropped, All-Star kept)
  S4 UTC date instead of Eastern              6 fail
  S5 an unplayed game's "0" read as a score   2 fail
  S6 neutral sites kept in home margins       1 fail
  S7 a level score graded PUSH                1 fail
  S8 slate fetch reads one UTC day's month    1 fail
Added 2026-10-31 with the real postponed shells (now 89 checks):
  P1 is_called_off dropped from grade         4 fail  (shells read PENDING)
  P2 "finished" keyed on state == "post"      7 fail  (shells read as 0-0
                                                       decided games; form
                                                       polluted, CHI/MIA at 3)
S3 first CRASHED (KeyError on NY, whose only counted game it dropped) rather
than reporting failures; the margin lookups were made tolerant so a broken
rule is named, not just a traceback.
"""

import copy
import datetime
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import espn_odds  # noqa: E402
import nba_grading  # noqa: E402
import team_meta  # noqa: E402
import yaml  # noqa: E402
from fetchers import nba  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
checks = {"pass": 0, "fail": 0}
failures = []


def ok(name, cond, detail=""):
    if cond:
        checks["pass"] += 1
    else:
        checks["fail"] += 1
        failures.append(name + (": " + str(detail) if detail else ""))


_FX = json.load(open(os.path.join(HERE, "nba_games_fixture.json")))
CASES = _FX["_provenance"]["cases"]
EVENTS = {k: v for k, v in _FX.items() if not k.startswith("_")}
ORDERED = [EVENTS[k] for k in sorted(EVENTS)]
TEAMS = {k: v for k, v in json.load(open(os.path.join(HERE, "nba_teams_fixture.json"))).items()
         if not k.startswith("_")}


# ------------------------------------------------- 1. the counted-game rule
# Expected set from the CAPTURE'S OWN LABELS: "STD ..." counts; PRESEASON,
# ALLSTAR and CC do not.
want = {k for k, label in CASES.items() if label.startswith("STD")}
got = {k for k, e in EVENTS.items() if nba.is_counted_game(e)}
ok("the counted set is exactly the capture's STD regular-season events (8 of 13)",
   got == want and len(want) == 8, sorted(got ^ want))
for k, label in CASES.items():
    if not label.startswith("STD"):
        ok("excluded: {} ({})".format(k, label.split(" --")[0]), not nba.is_counted_game(EVENTS[k]))
ok("both Cup semifinal and international game COUNT, though neutral",
   nba.is_counted_game(EVENTS["401809837"]) and nba.is_counted_game(EVENTS["401810002"])
   and EVENTS["401809837"]["competitions"][0]["neutralSite"]
   and EVENTS["401810002"]["competitions"][0]["neutralSite"])
ok("the Cup final is out though its teams are real and its season.type is 2",
   EVENTS["401809839"]["season"]["type"] == 2 and not nba.is_counted_game(EVENTS["401809839"]))
ok("preseason is out though its competition type is STD",
   EVENTS["401898396"]["competitions"][0]["type"]["abbreviation"] == "STD"
   and not nba.is_counted_game(EVENTS["401898396"]))


# ------------------------------------------------------ 2. Eastern slate dates
for k, utc, eastern in (("401810002", "2025-11-02T02:00Z", "2025-11-01"),   # EDT, crosses midnight
                        ("401809839", "2025-12-17T01:30Z", "2025-12-16"),   # EST, crosses midnight
                        ("401909907", "2026-11-03T00:00Z", "2026-11-02"),   # EST, crosses midnight
                        ("401909896", "2026-10-31T19:00Z", "2026-10-31")):  # afternoon tip
    ok("{} stamped {} is a {} game".format(k, utc, eastern),
       EVENTS[k]["date"] == utc and nba.slate_date(EVENTS[k]) == eastern, nba.slate_date(EVENTS[k]))
ok("an event with no timestamp has no slate date", nba.slate_date({"date": ""}) is None)
for k, e in EVENTS.items():
    d = datetime.date.fromisoformat(nba.slate_date(e))
    ok("season year by the ENDING year: {} on {} is season {}".format(k, d, e["season"]["year"]),
       nba.season_year_for_date(d) == e["season"]["year"], nba.season_year_for_date(d))

slate = nba_grading.slate_from_events(ORDERED, "2025-11-01")
ok("the 2025-11-01 slate holds the Mexico City game, found under the NEXT UTC day",
   list(slate) == ["401810002"], list(slate))
ok("  and 2025-11-02 holds nothing", nba_grading.slate_from_events(ORDERED, "2025-11-02") == {})
ok("the 2026-11-02 slate holds CLE vs OKC; 11-03 holds nothing",
   list(nba_grading.slate_from_events(ORDERED, "2026-11-02")) == ["401909907"]
   and nba_grading.slate_from_events(ORDERED, "2026-11-03") == {})
ok("the 2025-12-16 slate is EMPTY: the Cup final played that night is not graded",
   nba_grading.slate_from_events(ORDERED, "2025-12-16") == {})
ok("the 2026-02-15 slate is EMPTY: All-Star games are not graded",
   nba_grading.slate_from_events(ORDERED, "2026-02-15") == {})
ok("2026-10-28 holds both of that night's games",
   sorted(nba_grading.slate_from_events(ORDERED, "2026-10-28")) == ["401909881", "401909882"])


# -------------------------------------------- 3. form reads decided, counted games
rows = nba.counted_games(ORDERED)
ok("unplayed games parse with NO score, not 0-0",
   all(r["home_score"] is None for r in rows if r["id"] in ("401909896", "401909907")))
margin = nba.build_prior_season_margin(rows)
# By hand from the fixture's scores: ORL 120-132 NY, DET 122-110 DAL,
# ATL 117-129 MIA, CLE 121-111 WSH. One decided counted game each.
ok("prior margins: each team's own points margin over its decided counted games",
   margin == {"NY": {"margin": 12.0, "games": 1}, "ORL": {"margin": -12.0, "games": 1},
              "DET": {"margin": 12.0, "games": 1}, "DAL": {"margin": -12.0, "games": 1},
              "MIA": {"margin": 12.0, "games": 1}, "ATL": {"margin": -12.0, "games": 1},
              "CLE": {"margin": 10.0, "games": 1}, "WSH": {"margin": -10.0, "games": 1}}, margin)
ok("  ATL is 1 game: its PRESEASON win at IND did not leak in", margin.get("ATL", {}).get("games") == 1)
ok("  NY is 1 game: the Cup FINAL did not leak in", margin.get("NY", {}).get("games") == 1)
ok("  CLE is 1 game: its UNPLAYED game vs OKC did not count as 0-0", margin.get("CLE", {}).get("games") == 1)
ok("  no All-Star side has a margin", not {"STARS", "WORLD", "STRIPES"} & set(margin))
ok("home margins exclude neutral sites (ATL -12, CLE +10; not the semifinal or Mexico City)",
   sorted(nba.home_margins(rows)) == [-12, 10], nba.home_margins(rows))


# ------------------------------------- 3b. POSTPONED shells (real, 2025-26)
# Every postponed game of 2025-26: each passes the counted-game rule and
# reports state "post", completed False, scores "0"/"0". That is a 0-0 "final"
# to anything keying on state -- and a tie the NBA cannot produce.
_PP = json.load(open(os.path.join(HERE, "nba_postponed_fixture.json")))
PP_CASES = _PP["_provenance"]["cases"]
SHELLS = [_PP[k] for k, label in PP_CASES.items() if label.startswith("POSTPONED")]
ok("the capture holds 2025-26's four postponed shells", len(SHELLS) == 4, len(SHELLS))
ok("  every one PASSES the counted-game rule (so the rule alone is not 1,230)",
   all(nba.is_counted_game(e) for e in SHELLS))
ok("  every one reads state 'post' with completed False and '0'/'0' -- the trap",
   all(e["competitions"][0]["status"]["type"]["state"] == "post"
       and e["competitions"][0]["status"]["type"]["completed"] is False
       and {c["score"] for c in e["competitions"][0]["competitors"]} == {"0"} for e in SHELLS))
_pp_rows = nba.counted_games(SHELLS)


def _pp_pick(e):
    """A moneyline pick on the shell's own home team -- the side a 0-0 read
    as final would grade as a level score."""
    by = {c["homeAway"]: c["team"]["abbreviation"] for c in e["competitions"][0]["competitors"]}
    return {"side": by["home"], "away_abbr": by["away"], "home_abbr": by["home"],
            "bet_type": "moneyline"}


ok("  parsed with NO scores, so they are not decided",
   all(r["home_score"] is None and r["away_score"] is None for r in _pp_rows) and len(_pp_rows) == 4)
ok("  and contribute nothing to form or home margins",
   nba.build_prior_season_margin(_pp_rows) == {} and nba.home_margins(_pp_rows) == [])
for e in SHELLS:
    ok("{} grades POSTPONED, not a 0-0 level score".format(e["id"]),
       nba_grading.grade(_pp_pick(e), e, False)[1] == "POSTPONED")
    ok("  {} is still regular season, with no observed facts".format(e["id"]),
       nba_grading.season_phase(e) == "regular" and nba_grading.observed_facts(e) is None)
_makeup, _orig = _PP["401850920"], _PP["401810384"]
ok("the MAKEUP of 401810384 is a NEW event id, so a replay cannot be followed by id",
   _makeup["id"] != _orig["id"] and nba.is_counted_game(_makeup)
   and {c["team"]["abbreviation"] for c in _makeup["competitions"][0]["competitors"]}
   == {c["team"]["abbreviation"] for c in _orig["competitions"][0]["competitors"]})
ok("  and fetch_replay_dates answers nothing rather than a wrong date",
   nba_grading.fetch_replay_dates(None, {}, {"401810384"}) == {})
_all = nba.counted_games(SHELLS + [_PP["401850920"], _PP["401810547"]])
ok("counted 6, decided 2: only decided games may be counted",
   len(_all) == 6 and len(nba.build_prior_season_margin(_all)) == 2
   and all(v["games"] == 2 for v in nba.build_prior_season_margin(_all).values()),
   nba.build_prior_season_margin(_all))


# ----------------------------------------------------------------- 4. grading
def pick(side, away, home, bet_type="moneyline"):
    return {"side": side, "away_abbr": away, "home_abbr": home, "bet_type": bet_type}


g = nba_grading.grade
ok("MIA won at ATL 129-117: MIA is a HIT",
   g(pick("MIA", "MIA", "ATL"), EVENTS["401909881"], False)[1:] == ("HIT", "outcome"))
ok("  and ATL a MISS", g(pick("ATL", "MIA", "ATL"), EVENTS["401909881"], False)[1] == "MISS")
ok("CLE beat WSH 121-111: CLE is a HIT",
   g(pick("CLE", "WSH", "CLE"), EVENTS["401909882"], False)[1] == "HIT")
ok("an unplayed game is PENDING, not a 0-0 tie",
   g(pick("POR", "POR", "UTAH"), EVENTS["401909896"], False)[1] == "PENDING")
# NEVER-OBSERVED EDGE, one field: 401909882's scores set level.
level = copy.deepcopy(EVENTS["401909882"])
for c in level["competitions"][0]["competitors"]:
    c["score"] = "111"
ok("a LEVEL final is UNRESOLVED (a broken feed), never PUSH",
   g(pick("CLE", "WSH", "CLE"), level, False)[1] == "UNRESOLVED")
ok("a game missing from the slate is UNRESOLVED", g(pick("CLE", "WSH", "CLE"), None, False)[1] == "UNRESOLVED")
ok("a side naming neither team is UNRESOLVED",
   g(pick("BOS", "WSH", "CLE"), EVENTS["401909882"], False)[1] == "UNRESOLVED")
ok("a non-moneyline market is UNRESOLVED",
   g(pick("CLE", "WSH", "CLE", "spread"), EVENTS["401909882"], False)[1] == "UNRESOLVED")
facts = nba_grading.observed_facts(EVENTS["401809837"])
ok("observed facts carry the raw score and the neutral site",
   facts["home_score"] == 120 and facts["away_score"] == 132 and facts["result"] == "A"
   and facts["neutral_site"] is True, facts)
for k, want_phase in (("401909881", "regular"), ("401810002", "regular"), ("401898396", "preseason"),
                      ("401809839", None), ("401838140", None)):
    ok("season phase {} -> {}".format(k, want_phase),
       nba_grading.season_phase(EVENTS[k]) == want_phase, nba_grading.season_phase(EVENTS[k]))


# ------------------------------------------------------------- 5. prices
for k in ("401909896", "401909897", "401909898"):
    o = espn_odds.parse_event(EVENTS[k])
    ok("{} pre-game: both sides' close AND open moneylines parse".format(k),
       o and None not in (o["home_ml"], o["away_ml"], o["home_ml_open"], o["away_ml_open"]), o)
ok("401909907 pre-game with no odds block yet: no price, not an error",
   espn_odds.parse_event(EVENTS["401909907"]) is None)
ok("completed games carry no price at all (ESPN strips it)",
   espn_odds.parse_event(EVENTS["401909881"]) is None and espn_odds.parse_event(EVENTS["401909882"]) is None)
ok("espn_odds knows NBA's league path", espn_odds.LEAGUE_PATHS.get("nba") == "basketball/nba")


# --------------------------------------------------------------- 6. team_meta
ok("NBA_TEAMS is the /teams capture exactly: 30 keys, ESPN's abbreviations",
   set(team_meta.NBA_TEAMS) == set(TEAMS) and len(TEAMS) == 30)
ok("  each with ESPN's own colour",
   all(team_meta.NBA_TEAMS[k] == (k, "#" + v["color"].upper()) for k, v in TEAMS.items()),
   [k for k, v in TEAMS.items() if team_meta.NBA_TEAMS.get(k) != (k, "#" + v["color"].upper())])
ok("every counted game's teams have branding",
   all(team_meta.get_team_meta("nba", t) for r in rows for t in (r["home"], r["away"])))
ok("All-Star sides have none", not any(team_meta.get_team_meta("nba", t)
                                       for t in ("STARS", "WORLD", "STRIPES")))


# --------------------------------------------------- 7. requests (no network)
class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    """Answers each month query with the fixture events stamped in that month."""
    def __init__(self, events, pad_to=None):
        self.events, self.pad_to, self.params = events, pad_to, []

    def get(self, url, params=None, timeout=None):
        self.params.append(dict(params or {}))
        evs = [e for e in self.events if e["date"].replace("-", "")[:6] == params["dates"]]
        if self.pad_to:
            evs = (evs * self.pad_to)[:self.pad_to] or [ORDERED[0]] * self.pad_to
        return _Resp({"events": evs})


s = _Session(ORDERED)
season = nba.fetch_season_games(s, 2026)
ok("a season walk asks month by month, October to June: 9 requests",
   [p["dates"] for p in s.params] == ["2025{:02d}".format(m) for m in (10, 11, 12)]
   + ["2026{:02d}".format(m) for m in range(1, 7)], [p["dates"] for p in s.params])
ok("  each at limit 500, never a range", all(p["limit"] == 500 and "-" not in p["dates"] for p in s.params))
ok("  and keeps only the season's counted games (Mexico City, the semifinal)",
   sorted(r["id"] for r in season) == ["401809837", "401810002"], [r["id"] for r in season])
try:
    nba.fetch_season_games(_Session(ORDERED, pad_to=500), 2026)
    raised = False
except RuntimeError:
    raised = True
ok("a full page (500 events) raises rather than returning a truncated season", raised)

s = _Session(ORDERED)
cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml")))
got = nba_grading.fetch_slate(s, cfg, "2026-10-31")
ok("grading 10-31 asks for October AND November (a late game can stamp 11-01 UTC)",
   [p["dates"] for p in s.params] == ["202610", "202611"], [p["dates"] for p in s.params])
ok("  and returns exactly that Eastern day's three games",
   sorted(got) == ["401909896", "401909897", "401909898"], sorted(got))
ok("config's nba.scoreboard_url is the module's endpoint",
   cfg["nba"]["scoreboard_url"] == nba.SCOREBOARD_URL == nba_grading._scoreboard_url(cfg))
import generate_insights  # noqa: E402
import generate_stats  # noqa: E402
import signal_report  # noqa: E402
ok("NBA is in neither gate", "nba" not in (cfg.get("active_sports") or [])
   and "nba" not in (cfg.get("active_game_sports") or []))
ok("  and in no registry: no builder, fetcher or grading adapter until the backtest",
   "nba" not in generate_insights.GAME_BUILDERS and "nba" not in generate_stats.SPORT_FETCHERS
   and "nba" not in signal_report.SPORT_ADAPTERS)


print("nba: {} checks pass".format(checks["pass"]) if not checks["fail"]
      else "nba: {} PASS, {} FAIL".format(checks["pass"], checks["fail"]))
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if checks["fail"] else 0)
