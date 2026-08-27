"""Fetcher for college football (FBS) data -- a HYBRID of two sources, split
by what each one is actually good at:

  * Schedules -- sportsdataverse's cfbfastR-data, one plain CSV per season at
    raw.githubusercontent.com (`schedules/csv/cfb_schedules_{season}.csv`).
    Same `_get_csv`-over-`requests` shape fetchers/nfl.py uses for nflverse,
    and for the same reason: no cfbfastR/R dependency, no API key, and this
    project's dependency footprint stays requests + PyYAML. Crucially it
    carries `home_division`/`away_division`, which is the ONLY field in
    either source that cleanly separates FBS from FCS opponents -- see
    fbs_matchup_index below for why that matters so much here.
  * Team form (PPA off/def, turnovers) -- the CollegeFootballData API
    (api.collegefootballdata.com), which needs a free API key. Two bulk
    per-season calls, `/ppa/games` and `/games/teams`, both with a bare
    `year=` (no per-team fan-out) -- see the call-budget note on
    build_game_entities.

WHY HYBRID, rather than taking everything from CFBD: the schedule is the one
thing needed on every run regardless of whether team form resolves, and
taking it from a keyless static CSV means a missing/expired API key degrades
to "no signals" rather than "no slate". It also keeps the division fields and
the PPA rows from two independent publishers, so a disagreement between them
is visible rather than silently self-consistent.

THE API KEY IS READ FROM THE ENVIRONMENT ONLY -- os.environ, never
config.yaml, never a literal in this file. In CI it arrives as the
CFBD_API_KEY GitHub Actions repo secret. For a local or manual run, export it
in your shell first:

    export CFBD_API_KEY=...        # https://collegefootballdata.com/key

That plain-env-var convention is the one this repo already uses everywhere it
reads process state (fetchers/mlb.py, generate_insights.py,
capture_training_data.py); there is deliberately no .env file and no dotenv
dependency, because neither exists in this repo today.

NO QB-AVAILABILITY OVERRIDE, and that is an explicit decision rather than an
omission. fetchers/nfl.py carries one (get_starting_qb/qb_out) because
nflverse publishes a weekly injury report. College football has NO equivalent
public injury feed at all -- there is no conference-wide mandated injury
report, and CFBD publishes none -- so there is nothing to build the override
from. Inventing a proxy (e.g. "whoever started last week, assumed healthy")
would produce an override that never fires, which is strictly worse than not
having one: it would look like a modelled input while contributing nothing.
CFB moneyline is therefore scored from team form alone. See cfb_signals.py.

Feeds ONE pipeline, not two: build_game_entities() ->
generate_insights.GAME_BUILDERS, the scored moneyline market. There is no
fetch() here and no generate_stats.SPORT_FETCHERS entry -- CFB has no "Who's
Hot" leaderboard in this pass.

Still out of scope: every CFB bet type other than moneyline, and the weight
calibration (this PR ships flat placeholder weights; the backtest is its own
follow-up, exactly as NFL's was).
"""

import collections
import csv
import datetime
import io
import math
import os

import requests

import pulse
import team_meta

REQUEST_TIMEOUT = 20

# One plain git blob per season -- raw.githubusercontent.com serves it
# directly, no redirect-following and no key.
CFBFASTR_SCHEDULE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main"
    "/schedules/csv/cfb_schedules_{season}.csv"
)

CFBD_API_BASE = "https://api.collegefootballdata.com"
CFBD_KEY_ENV = "CFBD_API_KEY"

# HARD CEILING ON CFBD CALLS PER build_game_entities RUN.
#
# The free CFBD tier is 1,000 calls a MONTH, and this fetcher's steady-state
# cost is meant to be zero: every week is fetched once, cached per (season,
# week) in data/boxscores.json, and served from there forever after. The
# arithmetic only holds while that cache is being committed, and exactly one
# workflow commits it (daily-stats-and-grade.yml). If that workflow stops --
# and its crons have been dropped before, which is why deploy-pages.yml
# carries two hand-written alarms about it -- every run goes back to a cold
# cache and pays full price: at week 10 that is 10 calls a run, ~4 runs a day,
# ~1,200 calls a month. The quota would be gone mid-season with nothing
# saying so.
#
# 12 is deliberately ABOVE the honest worst case and below the runaway one. A
# genuinely cold cache at the deepest point of the season needs 1 bulk /ppa
# call plus one /games/teams per prior week, so a week-16 cold start is 16 --
# already over this, and that is the point: a cold start that deep means the
# cache is not working, and the right response is to degrade and say so rather
# than to spend the month's quota proving it. Everything healthy costs 0 or 2.
#
# Hitting it is NOT an exception. The remaining weeks return empty, form for
# those weeks is None, the affected signals drop out of the lean, and the
# slate still builds -- the same degradation a missing API key produces. What
# it must never do is fail silently, so it prints an Actions error annotation.
MAX_CFBD_CALLS_PER_RUN = 12

# ONLY A RUN THAT PERSISTS THE CACHE MAY SPEND CALLS.
#
# This is the rule that actually keeps the monthly quota safe, and the ceiling
# above is the backstop behind it. The arithmetic that forced it: two workflows
# run this fetcher, and only ONE of them commits data/boxscores.json.
# daily-stats-and-grade.yml does (twice a day). deploy-pages.yml does not -- it
# commits nothing at all, by design, being a deploy. So every CFBD call a
# deploy run makes is thrown away the moment the job ends, and the next deploy
# run makes it again. That is not a cache miss, it is a cache that cannot exist.
#
# Left alone, the ceiling alone does not save it: 12 calls x ~4 runs a day x 30
# days is ~1,440, over a 1,000/month tier, in a state nothing announces.
#
# So a run that cannot keep what it buys does not buy. Unset, this fetcher uses
# whatever the committed cache already holds and skips the fetch -- the same
# degradation week 1 and the ESPN fallback already produce. Set (by the one
# workflow that commits), the fetch happens and the result is kept forever.
#
# Consequence worth stating: if daily-stats-and-grade stops running, CFB form
# goes stale rather than the quota going to zero. That is the right way round.
CFBD_ALLOW_ENV = "CFB_ALLOW_CFBD"

_cfbd_calls = {"count": 0, "capped": False}


def cfbd_spending_allowed():
    """Whether this run may spend CFBD calls -- see CFBD_ALLOW_ENV."""
    return bool((os.environ.get(CFBD_ALLOW_ENV) or "").strip())


def reset_cfbd_budget():
    """Start a fresh per-run budget. Called at the top of build_game_entities
    so a long-lived process (the backtest sweeps three seasons in one) gets a
    fresh allowance per slate rather than tripping on the previous one."""
    _cfbd_calls["count"] = 0
    _cfbd_calls["capped"] = False


def cfbd_calls_used():
    """Calls spent in the current run -- read by tests and by the run log."""
    return _cfbd_calls["count"]

# cfbfastR's division values are lowercase ('fbs', 'fcs', 'ii', 'iii', and
# 'NA' for a handful of non-NCAA opponents). Only this one counts as FBS.
FBS_DIVISION = "fbs"

# How far ahead a slate reaches. Seven days because college football is played
# on Saturdays -- see the note in build_game_entities.
FIXTURE_WINDOW_DAYS = 7

# A week whose last scheduled kickoff is older than this counts as final even
# if some game never reported complete -- see final_regular_weeks. Two weeks is
# far longer than any in-week rescheduling window and far shorter than the
# offseason, so it can only ever promote genuinely-dead weeks.
STALE_WEEK_DAYS = 14


def _schedule_url(season):
    return CFBFASTR_SCHEDULE_URL.format(season=season)


def _num(v):
    """CSV/JSON values arrive as strings (or '' / None for blanks); this is
    the one place they become floats, or None for anything that isn't one --
    never a bare 0.0 standing in for "missing"."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_csv(session, url, required=True):
    """GET a CSV over plain requests, parsed via csv.DictReader. Returns []
    (not an error) for a 404 when `required=False` -- cfbfastR-data has no
    file at all for a season that has not started, which is a normal
    offseason state rather than a fetch failure. Mirrors
    fetchers/nfl.py's _get_csv exactly."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404 and not required:
        return []
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def _get_json(session, url, params):
    """GET JSON over plain requests. Keyless -- this is only used for the ESPN
    scoreboard fallback, which needs no credential, unlike _cfbd_get."""
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# CollegeFootballData API -- key from the environment, bulk per-season calls.
# --------------------------------------------------------------------------- #

def cfbd_key():
    """The CFBD API key from the environment, or a RuntimeError naming
    exactly what is missing and how to supply it.

    Deliberately os.environ.get + an explicit raise rather than a bare
    os.environ[...] subscript: the KeyError that produces says only
    'CFBD_API_KEY', with no hint that it is a free key from a specific site
    or that CI supplies it as a repo secret. The secret itself is never
    logged, only the variable's name."""
    key = os.environ.get(CFBD_KEY_ENV)
    if not key:
        raise RuntimeError(
            "{env} is not set. CFB team form comes from the CollegeFootballData "
            "API, which requires a free key (https://collegefootballdata.com/key). "
            "CI supplies it as a GitHub Actions repo secret; for a local or manual "
            "run, export it first:  export {env}=...".format(env=CFBD_KEY_ENV)
        )
    return key


def _cfbd_get(session, path, params):
    """One authenticated CFBD GET, returning parsed JSON (always a list for
    the endpoints used here). Raises on a non-2xx, same as the CSV path --
    a failed team-form fetch must not silently degrade to "every signal is
    None", which would look identical to a legitimately signal-less week 1.

    Refuses past MAX_CFBD_CALLS_PER_RUN. That refusal DOES return [] rather
    than raising, and the distinction is deliberate: a non-2xx is the API
    telling us something is wrong and must be loud, while the ceiling is us
    deciding to stop spending a monthly quota -- the slate should still build
    on whatever form was already gathered."""
    if not cfbd_spending_allowed():
        if not _cfbd_calls["capped"]:
            _cfbd_calls["capped"] = True
            print("insights(games): cfb {} is unset, so this run will not spend CFBD calls -- "
                  "it cannot commit data/boxscores.json and anything fetched would be "
                  "discarded and re-fetched next run. Serving whatever the committed cache "
                  "holds; form for uncached weeks is absent."
                  .format(CFBD_ALLOW_ENV))
        return []
    if _cfbd_calls["count"] >= MAX_CFBD_CALLS_PER_RUN:
        if not _cfbd_calls["capped"]:
            _cfbd_calls["capped"] = True
            msg = ("CFBD call ceiling reached ({} calls) -- refusing further requests this "
                   "run. Team form for the remaining weeks will be absent, so affected "
                   "signals drop out and those games score with less (or no) lean. This "
                   "means the per-week cache in data/boxscores.json is not being committed; "
                   "check that daily-stats-and-grade.yml is still running."
                   .format(MAX_CFBD_CALLS_PER_RUN))
            print("insights(games): cfb " + msg)
            if os.environ.get("GITHUB_ACTIONS"):
                print("::error title=CFB CFBD call ceiling hit::" + msg)
        return []
    _cfbd_calls["count"] += 1
    resp = session.get(
        CFBD_API_BASE + path,
        params=params,
        headers={"Authorization": "Bearer " + cfbd_key(),
                 "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_ppa_games(session, season, season_type="regular"):
    """Every (game, team) predicted-points-added row for a whole season, in
    ONE call with a bare `year=` -- no per-team fan-out.

    Each row carries `offense` (PPA this team's offense generated) and
    `defense` (PPA its defense ALLOWED) as nested objects with an `overall`
    key. Unlike nflverse's team stats -- where a team's defensive number has
    to be read off the OPPONENT's offensive row for the same game (see
    fetchers/nfl.build_team_form) -- CFBD publishes the allowed side
    directly, so no opponent join is needed here."""
    return _cfbd_get(session, "/ppa/games", {"year": season, "seasonType": season_type})


def get_team_game_stats(session, season, weeks, season_type="regular"):
    """Every team's per-game box stats for the given `weeks`, as a flat list.

    ONE CALL PER WEEK, and that is forced by the API rather than chosen:
    unlike /ppa/games, /games/teams REJECTS a bare `year=` with a 400
    ("either week, team, or conference are required") -- verified live
    against the real 2025 season, see the PR description. Of the three
    permitted fan-outs, week is the one used here because:

      * it is exhaustive by construction -- the week list is derived from the
        schedule already in hand, so it cannot under-enumerate the way a
        hand-maintained conference list could (a conference added or renamed
        would silently drop games);
      * it composes with point-in-time discipline for free -- a week-W game
        only ever needs weeks 1..W-1, so the fan-out shrinks to exactly the
        data that is legal to use, instead of fetching the full season and
        filtering afterwards;
      * ~11 conferences vs up to 15 prior weeks is close enough that
        correctness wins over the call count.

    Each element is one GAME carrying both teams, each with a `stats` list of
    {category, stat} pairs -- `turnovers` is the category this fetcher reads
    (that team's giveaways in that game)."""
    out = []
    for week in weeks:
        out.extend(_cfbd_get(session, "/games/teams",
                             {"year": season, "week": week, "seasonType": season_type}) or [])
    return out


# --------------------------------------------------------------------------- #
# Schedule.
# --------------------------------------------------------------------------- #

def season_for_date(game_date):
    """A college season is named for the year it STARTS in. The regular
    season runs late Aug-Dec and the postseason (bowls + the CFP final) runs
    into January, so a Jan/Feb date belongs to the PRIOR year's season -- the
    2025 season's title game was played 2026-01-19. March-August resolves to
    the season about to start that autumn. Same convention and same cut month
    as fetchers/nfl.season_for_date."""
    d = datetime.date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    return d.year if d.month >= 3 else d.year - 1


ESPN_CFB_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
                       "football/college-football/scoreboard")
# ESPN's group id for FBS (what it calls "FBS (I-A)"). Narrows the scoreboard,
# though it does NOT exclude an FBS team's FCS opponent -- see _espn_rows.
ESPN_FBS_GROUP = 80
# The scoreboard is a date-range endpoint with no season parameter, so a
# season is requested as a window. Aug 1 -> Feb 15 covers kickoff week through
# the championship without reaching the next season's opener.
ESPN_SEASON_WINDOW = ((8, 1), (2, 15))


def _espn_rows(session, season):
    """One season of ESPN scoreboard events, normalised into the SAME dict
    shape the cfbfastR CSV produces, so nothing downstream can tell which
    source it got.

    TEAM NAMES COME FROM `location`, NOT `displayName`, and that choice is the
    whole reason this fallback can exist. Everything in this module joins on
    the school name cfbfastR and CFBD both emit ("Alabama", "App State"), and
    ESPN's displayName is "Alabama Crimson Tide" -- 0 of 186 match. `location`
    matches all 136 CFB_TEAMS entries exactly, measured on a real slate.

    THAT SAME FACT SUPPLIES THE DIVISION FIELDS, which ESPN does not publish.
    A program in CFB_TEAMS is FBS; one that is not is FCS. Checked against a
    real opening weekend: the 50 unmatched names were Abilene Christian,
    Alcorn State, Furman and the like -- every one an FCS school. The known
    limitation is that a newly promoted FBS program missing from CFB_TEAMS
    reads as FCS until that table's yearly refresh, which is the same refresh
    its branding already depends on.
    """
    (m1, d1), (m2, d2) = ESPN_SEASON_WINDOW
    start = "{}{:02d}{:02d}".format(season, m1, d1)
    end = "{}{:02d}{:02d}".format(season + 1, m2, d2)
    payload = _get_json(session, ESPN_CFB_SCOREBOARD,
                        {"dates": "{}-{}".format(start, end),
                         "limit": 1000, "groups": ESPN_FBS_GROUP})
    rows = []
    for event in payload.get("events") or []:
        if (event.get("season") or {}).get("year") != season:
            continue
        comp = (event.get("competitions") or [{}])[0]
        sides = {}
        for c in comp.get("competitors") or []:
            team = c.get("team") or {}
            try:
                pts = int(c.get("score"))
            except (TypeError, ValueError):
                pts = None
            sides[c.get("homeAway")] = {"school": team.get("location"), "points": pts}
        if set(sides) != {"home", "away"} or not all(s["school"] for s in sides.values()):
            continue
        status = (comp.get("status") or {}).get("type") or {}
        rows.append({
            "game_id": str(event.get("id")),
            "season": str(season),
            "week": str((event.get("week") or {}).get("number") or ""),
            "start_date": event.get("date"),
            "completed": "TRUE" if status.get("completed") else "FALSE",
            "neutral_site": "TRUE" if comp.get("neutralSite") else "FALSE",
            "conference_game": "TRUE" if comp.get("conferenceCompetition") else "FALSE",
            "venue": (comp.get("venue") or {}).get("fullName"),
            "home_team": sides["home"]["school"],
            "away_team": sides["away"]["school"],
            "home_division": FBS_DIVISION if team_meta.get_team_meta(
                "cfb", sides["home"]["school"]) else "fcs",
            "away_division": FBS_DIVISION if team_meta.get_team_meta(
                "cfb", sides["away"]["school"]) else "fcs",
            "home_points": "" if sides["home"]["points"] is None else str(sides["home"]["points"]),
            "away_points": "" if sides["away"]["points"] is None else str(sides["away"]["points"]),
            # Not a cfbfastR column. Carried so build_game_entities can see
            # which source it got and refuse to spend CFBD calls on a join
            # that cannot match -- see SCHEDULE_SOURCE_KEY.
            SCHEDULE_SOURCE_KEY: "espn",
        })
    return rows


# Marks which publisher a schedule row came from. Present only on ESPN rows;
# a cfbfastR row simply lacks it, so `schedule_source()` defaults correctly
# for every row this module has ever produced.
SCHEDULE_SOURCE_KEY = "_source"


def schedule_source(schedule_rows):
    """'cfbfastr' or 'espn' -- whichever publisher produced these rows."""
    for row in schedule_rows or []:
        if row.get(SCHEDULE_SOURCE_KEY) == "espn":
            return "espn"
    return "cfbfastr"


def get_schedule(session, season):
    """Every scheduled game for one season, as dict rows.

    cfbfastR FIRST, ESPN as a fallback, and the order is not arbitrary:
    cfbfastR publishes the real division fields and shares CFBD's game ids, so
    the team-form join works against it and only against it. ESPN's ids are a
    different id space entirely.

    THE FALLBACK EXISTS BECAUSE cfbfastR PUBLISHES LATE. On 2026-08-27, two
    days before kickoff, cfb_schedules_2026.csv was still a 404 while ESPN
    already had all 99 opening-weekend games. Without this, turning CFB on for
    week 0 produces an empty Games tab and no error -- build_game_entities
    returns {} on an empty schedule, so the tab is silently blank on the one
    weekend it most needs to work.

    The fallback is safe precisely where it is needed and self-limiting after:
    week 0/1 has no prior form to fetch, so the id mismatch cannot matter, and
    build_game_entities refuses to spend CFBD calls at all while ESPN is the
    source. Once cfbfastR publishes, the primary takes over on the next run
    with no intervention."""
    rows = _get_csv(session, _schedule_url(season), required=False)
    if rows:
        return rows
    try:
        rows = _espn_rows(session, season)
    except Exception as e:  # noqa: BLE001 -- a failed fallback must not cost the run
        print("insights(games): cfb ESPN schedule fallback failed ({}: {}); "
              "no slate this run".format(type(e).__name__, str(e)[:160]))
        return []
    if rows:
        print("insights(games): cfb schedule from ESPN ({} rows) -- cfbfastR has not "
              "published season {} yet. Team form is UNAVAILABLE on this source (ESPN "
              "game ids do not match CFBD's), so no CFBD calls will be made."
              .format(len(rows), season))
    return rows


def et_date(start_date_utc):
    """The US-Eastern CALENDAR DATE of a UTC ISO kickoff, as 'YYYY-MM-DD'.

    This is not a formatting nicety, it is a correctness requirement, and it
    is the one place CFB's schedule differs dangerously from nflverse's.
    nflverse publishes a separate `gameday` column already in local terms;
    cfbfastR publishes only `start_date`, a UTC instant. Slicing that string
    (`start_date[:10]`) would file every night kickoff under the FOLLOWING
    day -- measured on the real 2025 season, 208 of 934 FBS-involved games
    (22.3%) have a UTC date that disagrees with their Eastern date, because
    college football's marquee window is Saturday night. A 7:00pm ET
    Saturday game is 23:00Z Saturday; an 8:00pm ET one is 00:00Z SUNDAY.
    Matching on the raw UTC prefix would move roughly a fifth of the slate
    to the wrong day.

    Returns None if unparseable, so a malformed row is skipped rather than
    silently landing on an arbitrary date."""
    if not start_date_utc:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.datetime.fromisoformat(str(start_date_utc).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except (ValueError, TypeError):
        return None


def get_games_on_date(session, game_date):
    """Every FBS-vs-FBS game kicking off on `game_date` (YYYY-MM-DD, US
    Eastern). Non-FBS matchups are excluded from the SLATE as well as from
    team form -- an FBS-vs-FCS buy game is not a market this scores."""
    season = season_for_date(game_date)
    schedule = get_schedule(session, season)
    return [r for r in schedule
            if et_date(r.get("start_date")) == game_date and _is_fbs_matchup(r)]


def _is_fbs_matchup(row):
    return (row.get("home_division") == FBS_DIVISION
            and row.get("away_division") == FBS_DIVISION)


def fbs_matchup_index(schedule_rows):
    """{game_id: {"week": int, "home": str, "away": str}} for FBS-vs-FBS
    REGULAR-SEASON games only. This single index is what enforces the
    division restriction on team form: every CFBD row is joined into it by
    game id, and anything not present -- an FBS-vs-FCS buy game, an
    FCS-vs-FCS game, a bowl -- is dropped before it can reach an average.

    Why this matters more in CFB than anywhere else in this repo: FBS teams
    schedule non-FBS opponents on purpose, and the results are lopsided by
    design. On the real 2025 season there were 126 such games, and letting
    them into a PPA average would inflate the form of exactly the teams that
    scheduled the weakest opponents -- a systematic bias, not noise.

    Postseason is excluded from FORM (bowls and playoff games are a
    different competitive context, and opt-outs make them unrepresentative),
    which is also what makes the point-in-time comparison in build_team_form
    safe: CFBD numbers postseason weeks from 1 again, so mixing the two
    season types into one `week` ordering would silently compare a bowl's
    week 1 against September."""
    index = {}
    for row in schedule_rows:
        if row.get("season_type") != "regular":
            continue
        if not _is_fbs_matchup(row):
            continue
        try:
            week = int(row["week"])
        except (TypeError, ValueError, KeyError):
            continue
        game_id = row.get("game_id")
        if not game_id:
            continue
        index[str(game_id)] = {"week": week,
                               "home": row.get("home_team"),
                               "away": row.get("away_team")}
    return index


# --------------------------------------------------------------------------- #
# Persisted team-form cache.
#
# WHY THIS IS WORTH IT: every /games/teams call this fetcher makes is for a
# week that is ALREADY FINAL. Point-in-time discipline means a week-W slate
# only ever fetches weeks 1..W-1, so the slate's own in-progress week is
# never requested at all. Measured on the real 2025 season: 7 of 7 fetched
# weeks final for a week-8 date, 13 of 13 for a week-14 date -- 100%
# cacheable, and re-fetched in full on every single run.
#
# WHERE IT LIVES: the cache dict generate_insights already hands every game
# builder and already persists. It loads data/boxscores.json, passes
# box_cache_all.get("cfb", {}) in as `boxscore_cache`, stores whatever the
# builder returns back under that key, and saves. CFB returned {} until now,
# so the channel was wired and unused. Reusing it means no new file, no new
# _load_store/_save_store, no circular import (fetchers/ cannot import
# generate_insights, which imports fetchers), and -- decisively -- no
# workflow change: data/boxscores.json is already in the daily workflow's
# `git add` list, so the cache survives the runner being torn down. A new
# file would silently never persist until that list was updated too.
#
# WHAT IT STORES: the reduced projection build_team_form actually reads, not
# the raw API response. That is the same choice data/boxscores.json already
# makes for MLB, which holds {gamePk: {teamId: {er, ip_outs}}} rather than
# raw boxscores. The size difference is not marginal: one week of raw
# /games/teams is ~398 KB, so a season is ~6.2 MB of weekly-churning
# committed JSON, against ~255 KB for the projection (4.0%). The largest
# data file in this repo today is 563 KB.
# --------------------------------------------------------------------------- #

def final_regular_weeks(schedule_rows, today=None):
    """Regular-season weeks in which every FBS-vs-FBS game is complete.

    Finality is judged on FBS-vs-FBS games only, because those are the only
    games build_team_form ever consumes -- fbs_matchup_index drops the rest
    before they can reach an average. A week whose FCS-vs-FCS filler is
    unreported is still safe to cache for our purposes. Measured on 2025 the
    two definitions agree anyway (weeks 1-13 final under both), so this is
    the looser rule only in principle.

    A week missing from this set is never written to the cache, so an
    in-progress or postponed week can never be frozen half-finished."""
    today = today or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    cutoff_date = (today - datetime.timedelta(days=STALE_WEEK_DAYS)).isoformat()

    by_week = {}
    for row in schedule_rows:
        if row.get("season_type") != "regular":
            continue
        if not _is_fbs_matchup(row):
            continue
        try:
            week = int(row["week"])
        except (TypeError, ValueError, KeyError):
            continue
        by_week.setdefault(week, []).append(row)
    final = set()
    for week, rows in by_week.items():
        if not rows:
            continue
        if all(str(r.get("completed", "")).upper() == "TRUE" for r in rows):
            final.add(week)
            continue
        # STALENESS FALLBACK. A completion flag alone leaves a permanent hole:
        # a CANCELLED game never flips to TRUE, so its week would be re-fetched
        # on every run forever. This is real, not hypothetical -- Liberty @ App
        # State on 2024-09-28 was cancelled (Hurricane Helene) and still reads
        # completed=FALSE with no score, which alone kept all of 2024 week 5
        # out of the cache.
        #
        # A game not played within STALE_WEEK_DAYS of its scheduled date is not
        # going to be. A genuine POSTPONEMENT is safe here because a rescheduled
        # game carries its new date in the schedule, so the week's latest
        # kickoff moves forward with it and the week stays non-final until the
        # new date has passed too.
        latest = max((et_date(r.get("start_date")) or "") for r in rows)
        if latest and latest < cutoff_date:
            final.add(week)
    return final


def _cache_bucket(cache, kind, season):
    return ((cache or {}).get(kind) or {}).get(str(season)) or {}


def _ppa_rows_from_cache(entry):
    """Rebuild the row shape build_team_form expects from the projection.
    Reconstructing rather than changing build_team_form's contract keeps the
    form math provably untouched by caching."""
    return [{"gameId": gid, "team": team, "offense": {"overall": off}, "defense": {"overall": dfn}}
            for gid, teams in entry.items() for team, (off, dfn) in teams.items()]


def _ppa_rows_to_cache(rows, week_of):
    """{week: {game_id: {team: [off, def]}}} for the given rows."""
    out = {}
    for r in rows:
        gid = str(r.get("gameId"))
        week = week_of.get(gid)
        if week is None:
            continue
        team = r.get("team")
        if not team:
            continue
        out.setdefault(week, {}).setdefault(gid, {})[team] = [
            _num((r.get("offense") or {}).get("overall")),
            _num((r.get("defense") or {}).get("overall")),
        ]
    return out


def _stats_rows_from_cache(entry):
    return [{"id": gid,
             "teams": [{"team": t, "stats": [{"category": "turnovers", "stat": v}]}
                       for t, v in teams.items()]}
            for gid, teams in entry.items()]


def _stats_rows_to_cache(games):
    out = {}
    for g in games or []:
        gid = str(g.get("id"))
        teams = {}
        for t in (g.get("teams") or []):
            name = t.get("team")
            if name:
                teams[name] = _team_turnovers(t)
        if teams:
            out[gid] = teams
    return out


def fetch_team_form_data(session, season, weeks, schedule_rows, cache=None):
    """(ppa_rows, team_stat_rows, updated_cache) for `weeks`, serving whatever
    the cache already holds and fetching only what it does not.

    Shared by build_game_entities and cfb_backtest.py so the two cannot
    diverge on cache semantics, the same reason form_cutoff is shared.

    Two different fetch strategies, because the endpoints differ:

      * /games/teams has NO bulk mode (a bare year= is a 400), so each
        missing week costs one call.
      * /ppa/games DOES accept a bare year=, so ANY number of missing weeks
        costs exactly ONE call -- the response is then split per week and
        cached. Per-week PPA fetching was verified to work and to match the
        bulk subset exactly, but it would turn a cold 13-week start into 13
        calls instead of 1, which is strictly worse for a first run and for
        the backtest's three-season sweep.

    Only FINAL weeks are written to the cache. A week still in progress is
    fetched and used but never stored, so it cannot be frozen incomplete."""
    cache = dict(cache or {})
    weeks = sorted(set(weeks))
    if not weeks:
        return [], [], cache
    final = final_regular_weeks(schedule_rows)
    week_of = {gid: entry["week"] for gid, entry in fbs_matchup_index(schedule_rows).items()}

    ppa_cached = dict(_cache_bucket(cache, "ppa", season))
    stats_cached = dict(_cache_bucket(cache, "games_teams", season))

    ppa_rows, stat_rows = [], []
    missing_ppa, missing_stats = [], []
    for w in weeks:
        key = str(w)
        if key in ppa_cached:
            ppa_rows.extend(_ppa_rows_from_cache(ppa_cached[key]))
        else:
            missing_ppa.append(w)
        if key in stats_cached:
            stat_rows.extend(_stats_rows_from_cache(stats_cached[key]))
        else:
            missing_stats.append(w)

    if missing_ppa:
        fresh = get_ppa_games(session, season)          # ONE call, all weeks
        split = _ppa_rows_to_cache(fresh, week_of)
        for w in missing_ppa:
            entry = split.get(w) or {}
            ppa_rows.extend(_ppa_rows_from_cache(entry))
            if w in final and entry:
                ppa_cached[str(w)] = entry

    for w in missing_stats:
        fresh = _cfbd_get(session, "/games/teams",
                          {"year": season, "week": w, "seasonType": "regular"}) or []
        entry = _stats_rows_to_cache(fresh)
        stat_rows.extend(_stats_rows_from_cache(entry))
        if w in final and entry:
            stats_cached[str(w)] = entry

    # Single-season retention: a stale season's weeks can never be needed
    # again by a live build, and keeping them would grow the committed store
    # without bound.
    cache["ppa"] = {str(season): ppa_cached}
    cache["games_teams"] = {str(season): stats_cached}
    return ppa_rows, stat_rows, cache


# --------------------------------------------------------------------------- #
# Team form -- season-to-date, point-in-time, FBS-vs-FBS only.
# --------------------------------------------------------------------------- #

def max_regular_week(fbs_index):
    """The highest regular-season week present in `fbs_index`, or 0 if it is
    empty. `fbs_index` is regular-season-only by construction, so this is
    "the last week that had been played by the end of the regular season"."""
    weeks = [e["week"] for e in fbs_index.values()]
    return max(weeks) if weeks else 0


def form_cutoff(game, max_reg_week):
    """The `upto_week` value that gives `game` a correct point-in-time form
    window.

    For a REGULAR-SEASON game this is just its own week: form must use
    strictly earlier weeks.

    For a POSTSEASON game it is max_reg_week + 1, i.e. the whole regular
    season, and getting this wrong is not a rounding error. CFBD renumbers
    postseason weeks from 1 -- every FBS-vs-FBS bowl and playoff game in a
    season carries week=1 -- so using the raw week would filter `fbs_index`
    down to games before regular-season week 1, which is nothing at all.
    Every form value would come back None and every bowl would score "No
    clear lean", which is exactly what this function was added to fix.

    The two filters are complementary and both are needed: fbs_matchup_index
    decides WHICH games may contribute to form (regular season, FBS-vs-FBS),
    and this decides HOW MANY of them a given game may see. cfb_backtest.py
    applies the same rule -- it is imported from here rather than duplicated,
    so the backtest and production cannot drift apart on it again."""
    if game.get("season_type") == "postseason":
        return max_reg_week + 1
    return int(game["week"])


def build_team_form(ppa_rows, team_stat_rows, fbs_index, upto_week):
    """Season-to-date per-game form for every FBS team, using only FBS-vs-FBS
    regular-season games STRICTLY BEFORE `upto_week`.

    Returns {team: {off_ppa, def_ppa_allowed, turnover_diff, games}}. A team
    absent from the return (or with a None field) has no qualifying games
    yet -- most commonly every team in week 1, and permanently for any team
    whose only games so far were against non-FBS opposition.

    Point-in-time discipline matches fetchers/nfl.build_team_form: a team's
    form ahead of week W must never include week W's own result. `fbs_index`
    supplies BOTH the week and the eligibility of every game, so the division
    filter and the point-in-time filter cannot disagree with each other.

    The three components:

      * off_ppa -- mean of `offense.overall` across qualifying games. PPA is
        already a per-play rate (CFBD's own EPA-equivalent), so unlike
        nflverse's passing_epa/rushing_epa -- which are per-game TOTALS
        needing a SUM(epa)/SUM(plays) reduction -- these average directly.
      * def_ppa_allowed -- mean of `defense.overall`, i.e. PPA this team's
        defense gave up. Published directly by CFBD, so no opponent join.
        LOWER is better; that sign flip lives in cfb_signals.SIGNAL_SPECS,
        not here.
      * turnover_diff -- mean per-game (takeaways - giveaways). CFBD's
        `turnovers` category is a team's own GIVEAWAYS in that game, so the
        differential is the OPPONENT's turnovers minus this team's, read off
        the two teams inside one /games/teams element."""
    ppa_by_team = {}
    for row in ppa_rows or []:
        entry = fbs_index.get(str(row.get("gameId")))
        if entry is None or entry["week"] >= upto_week:
            continue
        team = row.get("team")
        if not team:
            continue
        off = _num((row.get("offense") or {}).get("overall"))
        dfn = _num((row.get("defense") or {}).get("overall"))
        bucket = ppa_by_team.setdefault(team, {"off": [], "def": []})
        if off is not None:
            bucket["off"].append(off)
        if dfn is not None:
            bucket["def"].append(dfn)

    turnovers_by_team = {}
    for game in team_stat_rows or []:
        entry = fbs_index.get(str(game.get("id")))
        if entry is None or entry["week"] >= upto_week:
            continue
        teams = game.get("teams") or []
        if len(teams) != 2:
            continue
        giveaways = [_team_turnovers(t) for t in teams]
        if any(g is None for g in giveaways):
            continue
        for i, team_entry in enumerate(teams):
            # `team`, NOT `school` -- /games/teams names the column `team`,
            # matching /ppa/games and the schedule's home_team/away_team.
            # Reading `school` here returns None for every row and silently
            # yields zero turnover signal (verified live against 2025 week 1).
            name = team_entry.get("team")
            if not name:
                continue
            # Takeaways (what the opponent gave away) minus giveaways.
            turnovers_by_team.setdefault(name, []).append(giveaways[1 - i] - giveaways[i])

    out = {}
    for team in set(ppa_by_team) | set(turnovers_by_team):
        ppa = ppa_by_team.get(team) or {"off": [], "def": []}
        tos = turnovers_by_team.get(team) or []
        out[team] = {
            "off_ppa": round(sum(ppa["off"]) / len(ppa["off"]), 4) if ppa["off"] else None,
            "def_ppa_allowed": round(sum(ppa["def"]) / len(ppa["def"]), 4) if ppa["def"] else None,
            "turnover_diff": round(sum(tos) / len(tos), 4) if tos else None,
            "games": max(len(ppa["off"]), len(tos)),
        }
    return out


def _team_turnovers(team_entry):
    """That team's giveaways in one game, from the /games/teams `stats` list,
    or None when the category is absent (which CFBD does emit for some older
    or incomplete games -- a missing turnover count must not silently read
    as zero turnovers)."""
    for stat in team_entry.get("stats") or []:
        if stat.get("category") == "turnovers":
            return _num(stat.get("stat"))
    return None


def build_scoring_margins(schedule_rows, fbs_index, upto_week):
    """Season-to-date average point differential per team, over the same
    FBS-vs-FBS regular-season games before `upto_week` that team form uses.

    NOT a scored signal in v1 -- it carries no weight in
    config.betting_signals.cfb.bet_types, and cfb_signals.SIGNAL_SPECS does
    not define it. It is computed here and exposed in each game's `context`
    for one reason: NFL's calibration measured scoring margin as strongly
    collinear with offensive EPA and dropped it (see PR #35), so shipping it
    as a weighted CFB signal would repeat a finding this repo has already
    made. Carrying it in context costs one pass over an already-fetched
    schedule and gives the CFB backtest a measurable candidate to confirm or
    refute that for college football specifically."""
    totals = {}
    for row in schedule_rows:
        game_id = str(row.get("game_id") or "")
        entry = fbs_index.get(game_id)
        if entry is None or entry["week"] >= upto_week:
            continue
        home_pts, away_pts = _num(row.get("home_points")), _num(row.get("away_points"))
        if home_pts is None or away_pts is None:
            continue
        totals.setdefault(row["home_team"], []).append(home_pts - away_pts)
        totals.setdefault(row["away_team"], []).append(away_pts - home_pts)
    return {team: round(sum(v) / len(v), 3) for team, v in totals.items()}


# --------------------------------------------------------------------------- #
# Game insight entities -- registered into generate_insights.GAME_BUILDERS.
# --------------------------------------------------------------------------- #

def _fmt_ppa(v):
    return None if v is None else "{:+.2f}".format(v)


def _format_kickoff(start_date_utc):
    """UTC ISO kickoff -> 'H:MM AM/PM ET', mirroring
    fetchers/mlb._format_start_et (CFB's schedule is UTC like MLB's, not
    already-local like nflverse's)."""
    if not start_date_utc:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.datetime.fromisoformat(str(start_date_utc).replace("Z", "+00:00"))
        et = dt.astimezone(ZoneInfo("America/New_York"))
        hour = et.hour % 12 or 12
        return "{}:{:02d} {} ET".format(hour, et.minute, "AM" if et.hour < 12 else "PM")
    except Exception:  # noqa: BLE001 -- display nicety, never break the build
        return None


def _display_signals(away, home, away_form, home_form):
    """Team-relative framing chips, mirroring fetchers/nfl._display_signals:
    surface the single more-notable side per family."""
    signals = []
    ao, ho = away_form.get("off_ppa"), home_form.get("off_ppa")
    if ao is not None or ho is not None:
        if (ao if ao is not None else float("-inf")) > (ho if ho is not None else float("-inf")):
            signals.append({"label": "{} Off PPA/play".format(away), "value": _fmt_ppa(ao), "tone": "pos"})
        else:
            signals.append({"label": "{} Off PPA/play".format(home), "value": _fmt_ppa(ho), "tone": "pos"})
    ad, hd = away_form.get("def_ppa_allowed"), home_form.get("def_ppa_allowed")
    if ad is not None or hd is not None:
        # Lower allowed is better, so the HIGHER (worse) one is the side
        # worth flagging -- same framing rule NFL's def EPA chip uses.
        if (hd if hd is not None else float("-inf")) > (ad if ad is not None else float("-inf")):
            signals.append({"label": "{} Def PPA/play allowed".format(home), "value": _fmt_ppa(hd), "tone": "neg"})
        else:
            signals.append({"label": "{} Def PPA/play allowed".format(away), "value": _fmt_ppa(ad), "tone": "neg"})
    return signals


def _team_ref(school):
    """Branding for one program, mirroring fetchers/mlb._build_one_game's
    `teamref` -- the only other place the PICKS path (as opposed to
    generate_stats' leaderboard path) consumes team_meta.

    Degrades to exactly the previous behaviour for a program missing from
    CFB_TEAMS: abbr falls back to the school name and colour stays None,
    which is what every CFB entity carried before this existed. A program
    that joins FBS mid-refresh therefore renders plainly rather than
    breaking.

    Note the abbr this returns is also what gets used as the MARKET SIDE
    label (see _build_one_game), not just the chip text -- web/insights'
    sideColor() matches the leading token of `side` against these abbrs to
    tint a Signal Score row, so the two have to be the same string."""
    import team_meta  # local import, matching fetchers/mlb.py's convention
    meta = team_meta.get_team_meta("cfb", school) or {}
    return {"abbr": meta.get("abbr") or school,
            "name": school, "color": meta.get("color")}


def _build_one_game(config, g, form, margins):
    import cfb_signals

    away, home = g["away_team"], g["home_team"]
    away_ref, home_ref = _team_ref(away), _team_ref(home)
    # Form is keyed by SCHOOL (what CFBD and the schedule both emit); the
    # abbreviations below are presentation only and never a lookup key.
    away_form, home_form = form.get(away, {}), form.get(home, {})

    inputs = cfb_signals.build_inputs(
        away_abbr=away_ref["abbr"], home_abbr=home_ref["abbr"],
        away_off_ppa=away_form.get("off_ppa"), home_off_ppa=home_form.get("off_ppa"),
        away_def_ppa_allowed=away_form.get("def_ppa_allowed"),
        home_def_ppa_allowed=home_form.get("def_ppa_allowed"),
        away_turnover_diff=away_form.get("turnover_diff"),
        home_turnover_diff=home_form.get("turnover_diff"),
    )
    # No `availability` argument anywhere in this call: CFB has no injury
    # feed, so there is no QB override to apply. See the module docstring.
    betting = cfb_signals.score_game(config, "cfb", inputs)
    cfg = (config.get("betting_signals") or {}).get("cfb") or {}
    standout = cfb_signals.top_market(betting, cfg.get("standout_threshold", 50))

    market_labels = ((config.get("insights_ui") or {}).get("cfb") or {}).get("market_labels") or {}
    raw_markets = cfb_signals.list_markets(betting)
    signal_scores = [{"market": market_labels.get(m["bet_type"], m["bet_type"]),
                      "side": m["side"], "score": m["score"]} for m in raw_markets]
    if standout:
        standout = {**standout, "market": market_labels.get(standout.get("bet_type"), standout.get("bet_type"))}

    return {
        "gamePk": str(g["game_id"]),  # generic "id" field, reused across sports -- see generate_insights._build_games_section
        "sport": "cfb",
        # cfbfastR's schedule carries a `completed` flag but no in-progress
        # state, so like nflverse this is only ever "not yet played" or
        # "final" -- there is no "Live" this source can report.
        "status": "Final" if str(g.get("completed", "")).upper() == "TRUE" else "Preview",
        "away": away_ref,
        "home": home_ref,
        "start": _format_kickoff(g.get("start_date")),
        "venue": g.get("venue"),
        "probables": None,   # no starter feed for CFB -- see module docstring
        "signals": _display_signals(away_ref["abbr"], home_ref["abbr"], away_form, home_form),
        "pulse": None,       # no team_pulse.cfb config block
        "betting_signals": betting,
        "standout": standout,
        "best_angle": standout,
        "signal_scores": signal_scores,
        "compare": None,     # no insights_ui.cfb.compare_sets config
        "est_total": None,   # moneyline only, v1
        "f5_total": None,
        "context": {
            "away_team": away, "home_team": home,
            "week": g.get("week"),
            "neutral_site": g.get("neutral_site"),
            "conference_game": g.get("conference_game"),
            "away_off_ppa": away_form.get("off_ppa"), "home_off_ppa": home_form.get("off_ppa"),
            "away_def_ppa_allowed": away_form.get("def_ppa_allowed"),
            "home_def_ppa_allowed": home_form.get("def_ppa_allowed"),
            "away_turnover_diff": away_form.get("turnover_diff"),
            "home_turnover_diff": home_form.get("turnover_diff"),
            # Unweighted in v1 -- see build_scoring_margins' docstring.
            "away_scoring_margin": margins.get(away), "home_scoring_margin": margins.get(home),
            "away_games": away_form.get("games"), "home_games": home_form.get("games"),
        },
    }


def build_schedule_form(schedule_rows, season, before_date, window=3):
    """Per-program form computed from COMPLETED SCHEDULE ROWS ALONE.

    Deliberately separate from build_team_form, which is the CFBD/PPA path.
    This one costs NOTHING -- the schedule is already in hand and keyless -- and
    it exists because the Teams tab needs a number on every run, including the
    runs where CFBD is skipped (week 1, the ESPN fallback, or the call ceiling).
    A Teams tab that renders only when a quota allows it is not a tab.

    It is NOT a substitute for the scored model and does not feed it: points
    scored and allowed are cruder than PPA, which is exactly why PPA carries the
    weights. Nothing here reaches cfb_signals.

    Point-in-time by date, same discipline as everywhere else in this repo, and
    FBS-vs-FBS only -- letting the FCS buy games in would inflate the programs
    that scheduled the weakest opponents, the systematic bias fbs_matchup_index
    exists to prevent.
    """
    hist = collections.defaultdict(list)
    for row in schedule_rows or []:
        if str(row.get("season")) != str(season):
            continue
        if row.get("home_division") != FBS_DIVISION or row.get("away_division") != FBS_DIVISION:
            continue
        if str(row.get("completed", "")).upper() not in ("TRUE", "1"):
            continue
        date = et_date(row.get("start_date"))
        if not date or (before_date and date >= before_date):
            continue
        try:
            hp, ap = float(row["home_points"]), float(row["away_points"])
        except (TypeError, ValueError, KeyError):
            continue
        for team, pf, pa, venue in ((row["home_team"], hp, ap, "home"),
                                    (row["away_team"], ap, hp, "away")):
            hist[team].append({"pf": pf, "pa": pa, "venue": venue, "date": date,
                               "won": pf > pa})
    form = {}
    for team, past in hist.items():
        past.sort(key=lambda p: p["date"])
        n = len(past)
        recent = past[-window:]
        form[team] = {
            "played": n,
            "pf_pg": sum(p["pf"] for p in past) / n,
            "pa_pg": sum(p["pa"] for p in past) / n,
            "margin_pg": sum(p["pf"] - p["pa"] for p in past) / n,
            "recent_margin_pg": sum(p["pf"] - p["pa"] for p in recent) / len(recent),
            "wins": sum(1 for p in past if p["won"]),
            "losses": sum(1 for p in past if not p["won"]),
            "form_string": "".join("W" if p["won"] else "L" for p in recent),
        }
    return form


def _cfb_team_pulse(form, cfg):
    """Deterministic 0-100 notability score for ONE program, the same shape
    fetchers/mlb._team_pulse and fetchers/epl._team_pulse use: each component
    tanh-squashed against a league-average `base` by a `scale` that reads as a
    meaningful move, renormalized over whichever components have data.

    Direction is intrinsic and lives here, not in config -- more points scored
    is good, FEWER allowed is good.

    Returns None below the sample floor. CFB's floor bites harder than any
    other sport in this repo: a season is twelve games, so "three prior games"
    is a quarter of it, and there is no equivalent of MLB's 162 to average
    away a blowout. A program with fewer gets no pulse rather than a 50 that
    would look measured."""
    if not cfg or not form:
        return None
    if (form.get("played") or 0) < cfg.get("min_games", 3):
        return None
    terms = []
    for key, metric, favors_high in (("offense", "pf_pg", True), ("defense", "pa_pg", False)):
        block = cfg.get(key) or {}
        value = form.get(metric)
        if value is None or not block.get("scale"):
            continue
        delta = (value - block["base"]) if favors_high else (block["base"] - value)
        terms.append((math.tanh(delta / block["scale"]), block.get("weight", 0.5)))
    if not terms:
        return None
    wsum = sum(w for _, w in terms)
    if wsum <= 0:
        return None
    combined = sum(d * w for d, w in terms) / wsum
    return pulse.pulse(max(0, min(100, int(math.floor(50 + 50 * combined + 0.5)))))


def build_cfb_team_entities(config, form, slate_schools):
    """One Team card per program ON THIS SLATE, mirroring mlb and epl.

    Scoped to the slate rather than all 136 FBS programs on purpose: the Teams
    tab answers "who is playing today and how are they", which is the question
    the other sports' versions answer. A full FBS table is a different feature.
    """
    cfg = ((config.get("team_pulse") or {}).get("cfb") or {})
    if not cfg:
        return []
    out = []
    for school in slate_schools:
        f = form.get(school)
        if not f:
            continue
        ref = _team_ref(school)
        signals = []
        off_base = ((cfg.get("offense") or {}).get("base"))
        def_base = ((cfg.get("defense") or {}).get("base"))
        if f.get("pf_pg") is not None:
            signals.append({"label": "Points scored / game", "value": "%.1f" % f["pf_pg"],
                            "tone": "pos" if (off_base is None or f["pf_pg"] >= off_base) else "neg"})
        if f.get("pa_pg") is not None:
            signals.append({"label": "Points allowed / game", "value": "%.1f" % f["pa_pg"],
                            "tone": "pos" if (def_base is None or f["pa_pg"] <= def_base) else "neg"})
        signals.append({"label": "Record (FBS)", "value": "%d-%d" % (f["wins"], f["losses"]),
                        "tone": "pos" if f["wins"] >= f["losses"] else "neg"})
        if f.get("form_string"):
            signals.append({"label": "Last %d" % len(f["form_string"]),
                            "value": f["form_string"], "tone": "pos"})
        out.append({
            "id": school, "sport": "cfb", "abbr": ref.get("abbr"), "name": school,
            # `team_color`, the profile key -- game TeamRefs use `color`.
            "team_color": ref.get("color"),
            "pulse": _cfb_team_pulse(f, cfg),
            "signals": signals,
        })
    out.sort(key=lambda e: (-((e.get("pulse") or {}).get("score") or 0), e.get("abbr") or ""))
    return out


def build_game_entities(config, game_date, boxscore_cache, team_entities=None):
    """CFB's entry in generate_insights.GAME_BUILDERS -- same calling
    convention and return shape as fetchers.mlb/nfl.build_game_entities.

    CALL BUDGET: 2 + (C-1) network requests per run, where C is the largest
    form_cutoff on the slate -- one schedule CSV, one bulk /ppa/games for the
    season (bare `year=`), and one /games/teams per week the slate is allowed
    to see. It does not scale with slate size: every game on the date is
    built from those in memory.

    That is more than the 2-3 the design assumed, and the reason is measured
    rather than guessed: /games/teams rejects a bare `year=` (see
    get_team_game_stats). Real measured figures: week 1 costs 1 request (both
    CFBD calls skipped), a typical mid-season Saturday around week 8 costs 9,
    and a POSTSEASON slate costs 18 -- the most expensive case, because
    form_cutoff gives every bowl the whole regular season. The per-team
    fan-out this still avoids would be ~134 calls per season for a full FBS
    field.

    `boxscore_cache` carries the persisted team-form cache across runs (see
    the cache section above) and the updated cache is returned as the second
    value, which generate_insights writes back into data/boxscores.json under
    the "cfb" key. It is NOT a boxscore cache in MLB's sense -- CFB's signals
    come pre-aggregated from CFBD -- it reuses the same channel because the
    load/save/commit plumbing already exists and is already persisted. `team_entities` is likewise left untouched -- there is no
    team_pulse.cfb config to build a Team profile from.

    Returns (entities, {}, training_rows), keyed by CFBD's own game id as a
    string; training_rows is always [] (training_capture's schema is
    MLB-specific). Per-game failures are isolated exactly like MLB's and
    NFL's: one bad game is logged and skipped, the rest of the slate builds.
    """
    reset_cfbd_budget()
    session = requests.Session()
    season = season_for_date(game_date)
    schedule = get_schedule(session, season)
    if not schedule:
        return {}, {}, []

    # A WINDOW, NOT ONE DATE, for the same reason fetchers/epl uses one: college
    # football is a Saturday sport. A single-date slate leaves the Games tab
    # empty six days out of seven -- measured on the real 2026 opener, Thu 0,
    # Fri 0, Sat 6, Sun 0, Tue 0. Seven days always shows the week ahead, which
    # is the slate a reader actually wants on a Wednesday.
    #
    # Safe for the week logic below: form_cutoff is computed PER GAME, and the
    # comments there already anticipate a window straddling a week boundary.
    window_end = (datetime.date.fromisoformat(game_date)
                  + datetime.timedelta(days=FIXTURE_WINDOW_DAYS)).isoformat()
    games = [r for r in schedule
             if r.get("start_date") and _is_fbs_matchup(r)
             and game_date <= (et_date(r.get("start_date")) or "") <= window_end]
    if not games:
        return {}, {}, []

    fbs_index = fbs_matchup_index(schedule)
    max_reg_week = max_regular_week(fbs_index)
    # Fetch the union of prior weeks ONCE for the whole slate, rather than
    # per game: a single date is normally one week, but a week boundary can
    # split it, and re-fetching per game would multiply the call count by the
    # slate size for no new data.
    #
    # Driven by form_cutoff, NOT by the raw week: a bowl slate's raw weeks
    # are all 1, which would fetch nothing and leave every postseason game
    # with no form at all.
    cutoffs = {form_cutoff(g, max_reg_week) for g in games}
    max_cutoff = max(cutoffs)
    source = schedule_source(schedule)
    if max_cutoff <= 1:
        # Week 1 of the regular season: there are no prior games, so every
        # form value would be None regardless. Skip BOTH CFBD calls rather
        # than paying for data that cannot be used -- which also means an
        # opening-weekend slate builds with no API key at all, instead of
        # failing on a call whose result would have been discarded.
        ppa_rows, team_stats, form_cache = [], [], dict(boxscore_cache or {})
    elif source == "espn":
        # SAME REFUSAL, DIFFERENT REASON, and worth keeping separate from the
        # week-1 case above. Team form is joined to the schedule by GAME ID,
        # and ESPN's ids are a different id space from CFBD's -- every row
        # would be dropped by fbs_matchup_index and the fetch would buy
        # nothing. Spending a monthly quota to compute an empty join is the
        # worst of both outcomes, so this refuses outright and says why.
        print("insights(games): cfb week {} slate is on the ESPN fallback schedule -- "
              "skipping team form entirely (ESPN game ids cannot join CFBD rows). "
              "Games build with no form-based signals until cfbfastR publishes "
              "season {}.".format(max_cutoff, season))
        ppa_rows, team_stats, form_cache = [], [], dict(boxscore_cache or {})
    else:
        ppa_rows, team_stats, form_cache = fetch_team_form_data(
            session, season, range(1, max_cutoff), schedule, boxscore_cache)

    # One form table per distinct cutoff, not per game. A slate normally has
    # a single cutoff, so this is usually one build either way -- but a bowl
    # slate now aggregates the entire regular season, and repeating that per
    # game would redo the same full-season pass dozens of times.
    form_by_cutoff, margins_by_cutoff = {}, {}
    for cutoff in sorted(cutoffs):
        form_by_cutoff[cutoff] = build_team_form(ppa_rows, team_stats, fbs_index, cutoff)
        margins_by_cutoff[cutoff] = build_scoring_margins(schedule, fbs_index, cutoff)

    entities = {}
    for g in games:
        try:
            cutoff = form_cutoff(g, max_reg_week)
            form, margins = form_by_cutoff[cutoff], margins_by_cutoff[cutoff]
            entities[str(g["game_id"])] = _build_one_game(config, g, form, margins)
        except Exception as e:  # noqa: BLE001 -- one bad game must not cost the slate
            print("insights(games): cfb game {} ({} @ {}) failed to build ({}: {}); skipped"
                  .format(g.get("game_id"), g.get("away_team"), g.get("home_team"),
                          type(e).__name__, str(e)[:160]))

    if team_entities is not None:
        # Built from the SCHEDULE, never from CFBD -- see build_schedule_form.
        # So the Teams tab renders on every run, including the ones where the
        # team-form fetch was skipped entirely.
        schedule_form = build_schedule_form(schedule, season, game_date)
        schools = []
        for g in games:
            for key in ("home_team", "away_team"):
                if g.get(key) and g[key] not in schools:
                    schools.append(g[key])
        team_entities.extend(build_cfb_team_entities(config, schedule_form, schools))

    return entities, form_cache, []
