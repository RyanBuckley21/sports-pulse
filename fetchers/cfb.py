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

import csv
import datetime
import io
import os

import requests

REQUEST_TIMEOUT = 20

# One plain git blob per season -- raw.githubusercontent.com serves it
# directly, no redirect-following and no key.
CFBFASTR_SCHEDULE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main"
    "/schedules/csv/cfb_schedules_{season}.csv"
)

CFBD_API_BASE = "https://api.collegefootballdata.com"
CFBD_KEY_ENV = "CFBD_API_KEY"

# cfbfastR's division values are lowercase ('fbs', 'fcs', 'ii', 'iii', and
# 'NA' for a handful of non-NCAA opponents). Only this one counts as FBS.
FBS_DIVISION = "fbs"


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
    None", which would look identical to a legitimately signal-less week 1."""
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


def get_schedule(session, season):
    """Every scheduled game for one season, as raw CSV dict rows.

    One file per season here, unlike nflverse's single all-seasons games.csv,
    so a caller spanning seasons fetches once per season rather than once
    total. Not `required` -- a season with no published file yet returns []."""
    return _get_csv(session, _schedule_url(season), required=False)


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
# Team form -- season-to-date, point-in-time, FBS-vs-FBS only.
# --------------------------------------------------------------------------- #

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


def _build_one_game(config, g, form, margins):
    import cfb_signals

    away, home = g["away_team"], g["home_team"]
    away_form, home_form = form.get(away, {}), form.get(home, {})

    inputs = cfb_signals.build_inputs(
        away_abbr=away, home_abbr=home,
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
        # No team_meta table for CFB (130+ FBS programs, and no branding pass
        # in this PR), so name doubles as abbr and color is None -- the same
        # posture fetchers/nfl.py shipped with before NFL_TEAMS existed.
        "away": {"abbr": away, "name": away, "color": None},
        "home": {"abbr": home, "name": home, "color": None},
        "start": _format_kickoff(g.get("start_date")),
        "venue": g.get("venue"),
        "probables": None,   # no starter feed for CFB -- see module docstring
        "signals": _display_signals(away, home, away_form, home_form),
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


def build_game_entities(config, game_date, boxscore_cache, team_entities=None):
    """CFB's entry in generate_insights.GAME_BUILDERS -- same calling
    convention and return shape as fetchers.mlb/nfl.build_game_entities.

    CALL BUDGET: 2 + (W-1) network requests per run, where W is the latest
    week on the slate -- one schedule CSV, one bulk /ppa/games for the season
    (bare `year=`), and one /games/teams per PRIOR week. It does not scale
    with slate size: every game on the date is built from those in memory.

    That is more than the 2-3 the design assumed, and the reason is measured
    rather than guessed: /games/teams rejects a bare `year=` (see
    get_team_game_stats). A typical mid-season Saturday is week ~8, so ~9
    requests; the week-16 worst case is 17. The per-team fan-out this still
    avoids would be ~134 calls per season for a full FBS field.

    `boxscore_cache` is accepted for interface parity but unused, and the
    second return value is always {}: CFB's signals come pre-aggregated from
    CFBD, so there is no per-game boxscore to reconstruct and cache across
    runs. `team_entities` is likewise left untouched -- there is no
    team_pulse.cfb config to build a Team profile from.

    Returns (entities, {}, training_rows), keyed by CFBD's own game id as a
    string; training_rows is always [] (training_capture's schema is
    MLB-specific). Per-game failures are isolated exactly like MLB's and
    NFL's: one bad game is logged and skipped, the rest of the slate builds.
    """
    session = requests.Session()
    season = season_for_date(game_date)
    schedule = get_schedule(session, season)
    if not schedule:
        return {}, {}, []

    games = [r for r in schedule
             if et_date(r.get("start_date")) == game_date and _is_fbs_matchup(r)]
    if not games:
        return {}, {}, []

    fbs_index = fbs_matchup_index(schedule)
    # Fetch the union of prior weeks ONCE for the whole slate, rather than
    # per game: a single date is normally one week, but a week boundary can
    # split it, and re-fetching per game would multiply the call count by the
    # slate size for no new data.
    max_week = max(int(g["week"]) for g in games)
    if max_week <= 1:
        # Week 1: there are no prior games, so every form value would be None
        # regardless. Skip BOTH CFBD calls rather than paying for data that
        # cannot be used -- which also means an opening-weekend slate builds
        # with no API key at all, instead of failing on a call whose result
        # would have been discarded.
        ppa_rows, team_stats = [], []
    else:
        ppa_rows = get_ppa_games(session, season)
        team_stats = get_team_game_stats(session, season, range(1, max_week))

    entities = {}
    for g in games:
        try:
            week = int(g["week"])
            form = build_team_form(ppa_rows, team_stats, fbs_index, week)
            margins = build_scoring_margins(schedule, fbs_index, week)
            entities[str(g["game_id"])] = _build_one_game(config, g, form, margins)
        except Exception as e:  # noqa: BLE001 -- one bad game must not cost the slate
            print("insights(games): cfb game {} ({} @ {}) failed to build ({}: {}); skipped"
                  .format(g.get("game_id"), g.get("away_team"), g.get("home_team"),
                          type(e).__name__, str(e)[:160]))

    return entities, {}, []
