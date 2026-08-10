"""Fetcher for NFL data via nflverse's versioned CSV releases (CC BY 4.0).

Two sources, both plain CSV over `requests` -- no nflreadpy/nfl_data_py
dependency (nfl_data_py is archived as of Sept 2025; nflreadpy pulls in
polars, and this project's dependency footprint is deliberately just
requests + PyYAML, matching fetchers/mlb.py's own style):

  * Schedules -- nflverse/nfldata's `data/games.csv`, fetched from
    raw.githubusercontent.com (a plain git blob, not a GitHub Release asset,
    so it needs no redirect-following). One row per game, seasons
    1999-present, future weeks included with blank scores. Carries
    home_rest/away_rest directly -- no derivation needed, unlike MLB's OPS
    windows. LIVE-VERIFIED against the real 2025 season while building this
    fetcher (see the PR description).
  * Team weekly stats and injuries -- nflverse-data's `stats_team` and
    `injuries` GitHub Releases, one CSV per season each
    (`stats_team_week_{season}.csv`, `injuries_{season}.csv`). URL
    construction verified against nflreadr's own R source
    (github.com/nflverse/nflreadr/R/load_stats.R, load_injuries.R) and
    live-fetched against the real 2025 season while building this fetcher --
    also see the PR description for the exact columns confirmed.

Attribution: this data is nflverse's, released under CC BY 4.0. The site's
credits/footer needs a visible "NFL data via nflverse (CC BY 4.0)" line
before this fetcher's output ships to production -- not added here, since
that is a site-wide presentation change outside this module's job.

Scope: schedule + team-form signals for the MONEYLINE market only (see
nfl_signals.py) -- CFB, and every other NFL bet type, are deliberately not
this pass. This module does NOT build the stat_categories "who's hot"
leaderboard pipeline fetchers/mlb.py's fetch() feeds (see docs/leagues.md);
nothing here registers into generate_stats.SPORT_FETCHERS. It registers only
into generate_insights.GAME_BUILDERS, the games/Signal-Score path the
precursor PR made sport-aware.
"""

import csv
import datetime
import io

import requests

REQUEST_TIMEOUT = 20

# A plain git blob (not a Release asset) -- raw.githubusercontent.com serves
# it directly, one fetch covers every season on file.
NFLDATA_SCHEDULES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
# Release assets: one CSV per season, generated from that season's completed
# games -- see _get_csv's 404-as-[] handling for a season with none yet.
NFLVERSE_RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"


def _team_stats_url(season):
    return "{}/stats_team/stats_team_week_{}.csv".format(NFLVERSE_RELEASES, season)


def _injuries_url(season):
    return "{}/injuries/injuries_{}.csv".format(NFLVERSE_RELEASES, season)


def _num(v):
    """CSV values arrive as strings (or '' for blank/unplayed); this is the
    one place that turns them into floats, or None for anything that isn't
    one -- never a bare 0.0 standing in for "missing"."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_csv(session, url, required=True):
    """GET a CSV over plain requests, parsed via csv.DictReader.

    Returns [] (not an error) for a 404 when `required=False` -- the one
    genuinely expected shape of "missing" this fetcher has to tolerate: a
    season with no completed weeks yet has no stats_team/injuries release
    file at all (the file is GENERATED from completed games), which is a
    normal early-season or preseason state, not a fetch failure. The
    schedules fetch never expects this (games.csv always exists) and stays
    required=True, matching fetchers/mlb.py's convention of raising on a
    genuinely unexpected response rather than degrading silently."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404 and not required:
        return []
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def season_for_date(game_date):
    """nflverse's season convention: a season is named for the year it
    STARTS in. The regular season runs Sept-Dec and the playoffs run into
    Jan/Feb of the following calendar year (the 2025 season's Super Bowl was
    played 2026-02-08, still season "2025"), so a Jan/Feb date belongs to
    the PRIOR year's season. March-August (the offseason) resolves to the
    season about to start that autumn, so a preseason or backtest date in
    that range still finds the right schedule, even if it's mostly future
    rows."""
    d = datetime.date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    return d.year if d.month >= 3 else d.year - 1


def get_schedule(session, season=None):
    """Every row of nflverse/nfldata's games.csv, optionally filtered to one
    season. One fetch covers 1999-present (including future, unplayed
    weeks), so a caller needing several seasons -- a rolling team-form
    window spanning a season boundary, a future backtest -- should fetch
    once and filter client-side rather than re-fetching per season."""
    rows = _get_csv(session, NFLDATA_SCHEDULES_URL)
    if season is None:
        return rows
    season_s = str(season)
    return [r for r in rows if r.get("season") == season_s]


def get_games_on_date(session, game_date):
    """Every scheduled game whose `gameday` is exactly `game_date`
    (YYYY-MM-DD) -- the NFL equivalent of MLB's one-date schedule call,
    except NFL's slate is weekly (mostly Sunday, plus Thursday/Monday/an
    occasional Saturday late in the season), so most calendar dates return
    []."""
    season = season_for_date(game_date)
    return [r for r in get_schedule(session, season) if r.get("gameday") == game_date]


def get_team_stats(session, season):
    """Every (team, week) row of that season's nflfastR-derived team stats
    (EPA/play, turnovers, etc. -- see build_team_form), or [] if the season
    has no completed weeks yet."""
    return _get_csv(session, _team_stats_url(season), required=False)


def get_injuries(session, season):
    """Every (player, week) injury-report row for that season (practice
    participation + game status), or [] if none published yet."""
    return _get_csv(session, _injuries_url(season), required=False)


# --------------------------------------------------------------------------- #
# Team form -- season-to-date, point-in-time (only weeks strictly before the
# game being scored), from the two fetched tables above.
# --------------------------------------------------------------------------- #

def build_team_form(team_stats_rows, upto_week):
    """Season-to-date per-game form for every team appearing in
    `team_stats_rows`, using only weeks STRICTLY BEFORE `upto_week` -- the
    same point-in-time discipline fetchers/mlb.py's byDateRange calls
    enforce (a team's form ahead of week W must never include week W's own
    result). Returns {team: {off_epa, def_epa_allowed, turnover_diff,
    games}}; a team absent from the return (or with a None field) has no
    qualifying games yet -- most commonly, every team in week 1.

    def_epa_allowed is not a column nflverse publishes -- team-level rows are
    attributed to the team that produced them (an offense's EPA is THAT
    team's passing_epa/rushing_epa), so a team's defensive efficiency has to
    be read off the OPPONENT's offensive row for the same game -- `team` and
    `opponent_team` share a `game_id`/week, which makes this a same-table
    lookup rather than a second fetch. This is a real derived signal from two
    already-fetched real numbers, not an invented one.

    passing_epa/rushing_epa are PER-GAME TOTALS, not per-play rates (verified
    live: a real IND week-9 row carried passing_epa -16.26 over 50 attempts)
    -- so both off_epa and def_epa_allowed are computed here as SUM(epa) /
    SUM(plays) across the qualifying weeks (attempts + carries), a
    volume-weighted per-play rate, not an average of per-game totals or an
    average of per-game rates. Getting this wrong would silently mismatch
    nfl_signals.py's off_epa_gap/def_epa_gap scales, which are sized for a
    genuine per-play EPA range (~+/-0.15); a few games' worth of raw totals
    would saturate that tanh on nearly every game.

    v1 applies NO shrinkage/stabilization toward a league baseline (unlike
    fetchers/mlb.py's shrink()/bullpen_prior(), whose constants were measured
    against months of this repo's own production data). Doing that here
    would mean guessing a k the same way the config weights were told not to
    be guessed -- it's a backtest-measured decision, deferred with the rest
    of calibration."""
    by_team_week = {}
    for row in team_stats_rows:
        if row.get("season_type") != "REG":
            continue
        try:
            week = int(row["week"])
        except (TypeError, ValueError, KeyError):
            continue
        if week >= upto_week:
            continue
        by_team_week.setdefault(row.get("team"), {})[week] = row

    def _plays(row):
        return (_num(row.get("attempts")) or 0.0) + (_num(row.get("carries")) or 0.0)

    def _epa_total(row):
        return (_num(row.get("passing_epa")) or 0.0) + (_num(row.get("rushing_epa")) or 0.0)

    out = {}
    for team, weeks in by_team_week.items():
        n = len(weeks)
        if not n:
            continue
        off_epa_sum = sum(_epa_total(r) for r in weeks.values())
        off_plays_sum = sum(_plays(r) for r in weeks.values())

        def_epa_sum = def_plays_sum = 0.0
        turnover_vals = []
        for wk, row in weeks.items():
            opp_row = by_team_week.get(row.get("opponent_team"), {}).get(wk)
            if opp_row is not None:
                def_epa_sum += _epa_total(opp_row)
                def_plays_sum += _plays(opp_row)
            # Takeaways: interceptions this defense made + fumbles it
            # recovered from the opponent. Giveaways: interceptions this
            # offense threw + fumbles it lost -- fumbles_lost_total is the
            # pre-summed column (sack/rushing/receiving fumbles lost
            # combined), used directly rather than re-summed here.
            takeaways = (_num(row.get("def_interceptions")) or 0.0) + (_num(row.get("fumble_recovery_opp")) or 0.0)
            giveaways = (_num(row.get("passing_interceptions")) or 0.0) + (_num(row.get("fumbles_lost_total")) or 0.0)
            turnover_vals.append(takeaways - giveaways)
        out[team] = {
            "off_epa": round(off_epa_sum / off_plays_sum, 4) if off_plays_sum else None,
            "def_epa_allowed": round(def_epa_sum / def_plays_sum, 4) if def_plays_sum else None,
            "turnover_diff": round(sum(turnover_vals) / len(turnover_vals), 4) if turnover_vals else None,
            "games": n,
        }
    return out


def build_scoring_margins(schedule_rows, upto_week):
    """Season-to-date average point differential per team, from completed
    (scored) games strictly before `upto_week` -- straight off the
    schedule's own away_score/home_score, independent of team_stats
    entirely (a team could in principle have a scoring-margin reading with
    no team_stats row at all, though in practice the two sources agree on
    which games are complete)."""
    totals = {}
    for row in schedule_rows:
        if row.get("game_type") != "REG":
            continue
        try:
            week = int(row["week"])
        except (TypeError, ValueError, KeyError):
            continue
        if week >= upto_week:
            continue
        a_score, h_score = _num(row.get("away_score")), _num(row.get("home_score"))
        if a_score is None or h_score is None:
            continue
        totals.setdefault(row["away_team"], []).append(a_score - h_score)
        totals.setdefault(row["home_team"], []).append(h_score - a_score)
    return {team: round(sum(vals) / len(vals), 3) for team, vals in totals.items()}


# --------------------------------------------------------------------------- #
# QB availability -- the NFL analog of MLB's probable-starter-out override.
# --------------------------------------------------------------------------- #

def get_starting_qb(schedule_rows, team, before_week):
    """This team's most recently ANNOUNCED starting QB as of `before_week`.

    nflverse's schedule only fills away_qb_id/home_qb_id in AFTER a game goes
    Final (who actually started), not as a pregame projection the way MLB's
    probablePitcher hydrate is -- there is no equivalent "probable QB" feed
    in this data source. The best available proxy is "whoever started last
    time", refined against the CURRENT week's injury report by the caller
    (qb_out below). This is a real, clearly-flagged approximation, not a
    fabricated one: it is exactly the heuristic a human bettor would use
    absent an official pregame announcement.

    Returns (qb_id, qb_name) from the most recent PLAYED game strictly
    before `before_week`, or (None, None) if this team has no played games
    yet this season (most commonly, week 1)."""
    played = []
    for row in schedule_rows:
        if row.get("game_type") != "REG":
            continue
        try:
            week = int(row["week"])
        except (TypeError, ValueError, KeyError):
            continue
        if week >= before_week:
            continue
        if row.get("away_team") == team and row.get("away_qb_id"):
            played.append((week, row["away_qb_id"], row.get("away_qb_name")))
        elif row.get("home_team") == team and row.get("home_qb_id"):
            played.append((week, row["home_qb_id"], row.get("home_qb_name")))
    if not played:
        return None, None
    played.sort(key=lambda t: t[0])
    return played[-1][1], played[-1][2]


def qb_out(injury_rows, qb_id, week):
    """Whether `qb_id`'s injury report for `week` says Out or Doubtful.
    Questionable is deliberately NOT treated as out -- same bar MLB's
    availability override uses (an announced absence, not a game-time-
    decision tag the player likely plays through)."""
    if not qb_id:
        return False
    for row in injury_rows:
        if row.get("gsis_id") != qb_id:
            continue
        try:
            if int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue
        if row.get("report_status") in ("Out", "Doubtful"):
            return True
    return False


# --------------------------------------------------------------------------- #
# Game insight entities -- fetchers.mlb.build_game_entities' NFL counterpart,
# registered into generate_insights.GAME_BUILDERS.
# --------------------------------------------------------------------------- #

def _fmt_epa(v):
    return None if v is None else "{:+.2f}".format(v)


def _format_kickoff(gameday, gametime):
    """'H:MM AM/PM ET' from nflverse's separate gameday (date) + gametime
    columns. nflverse documents gametime as already Eastern (unlike MLB's
    UTC gameDate, which fetchers/mlb.py converts) -- corroborated here by a
    live-fetched row (a Thursday-night kickoff read 20:20, matching that
    window's well-known 8:20pm ET slot) rather than independently verified
    against nflverse's own docs. None if either input is missing or
    unparseable."""
    if not gameday or not gametime:
        return None
    try:
        hh, mm = (int(x) for x in gametime.split(":")[:2])
        hour = hh % 12 or 12
        return "{}:{:02d} {} ET".format(hour, mm, "AM" if hh < 12 else "PM")
    except (ValueError, AttributeError):
        return None


def _display_signals(away, home, away_form, home_form):
    """Team-relative framing chips, mirroring fetchers/mlb.py's `signals`
    list: surface the single more-notable side per family."""
    signals = []
    ao, ho = away_form.get("off_epa"), home_form.get("off_epa")
    if ao is not None or ho is not None:
        if (ao if ao is not None else float("-inf")) > (ho if ho is not None else float("-inf")):
            signals.append({"label": "{} Off EPA/play".format(away), "value": _fmt_epa(ao), "tone": "pos"})
        else:
            signals.append({"label": "{} Off EPA/play".format(home), "value": _fmt_epa(ho), "tone": "pos"})
    ad, hd = away_form.get("def_epa_allowed"), home_form.get("def_epa_allowed")
    if ad is not None or hd is not None:
        # Lower allowed is better, so the HIGHER (worse) one is the side
        # worth flagging -- same framing rule MLB's bullpen ERA (7d) uses.
        if (hd if hd is not None else float("-inf")) > (ad if ad is not None else float("-inf")):
            signals.append({"label": "{} Def EPA/play allowed".format(home), "value": _fmt_epa(hd), "tone": "neg"})
        else:
            signals.append({"label": "{} Def EPA/play allowed".format(away), "value": _fmt_epa(ad), "tone": "neg"})
    return signals


def _build_one_game(config, g, schedule, team_stats, injuries):
    import nfl_signals

    away, home = g["away_team"], g["home_team"]
    week = int(g["week"])

    form = build_team_form(team_stats, week)
    margins = build_scoring_margins(schedule, week)
    away_form, home_form = form.get(away, {}), form.get(home, {})

    away_qb_id, away_qb_name = get_starting_qb(schedule, away, week)
    home_qb_id, home_qb_name = get_starting_qb(schedule, home, week)
    availability = {
        "away_qb_out": qb_out(injuries, away_qb_id, week),
        "home_qb_out": qb_out(injuries, home_qb_id, week),
    }

    inputs = nfl_signals.build_inputs(
        away_abbr=away, home_abbr=home,
        away_off_epa=away_form.get("off_epa"), home_off_epa=home_form.get("off_epa"),
        away_def_epa_allowed=away_form.get("def_epa_allowed"), home_def_epa_allowed=home_form.get("def_epa_allowed"),
        away_turnover_diff=away_form.get("turnover_diff"), home_turnover_diff=home_form.get("turnover_diff"),
        away_scoring_margin=margins.get(away), home_scoring_margin=margins.get(home),
        away_rest=_num(g.get("away_rest")), home_rest=_num(g.get("home_rest")),
    )
    betting = nfl_signals.score_game(config, "nfl", inputs, availability=availability)
    standout_threshold = ((config.get("betting_signals") or {}).get("nfl") or {}).get("standout_threshold", 50)
    standout = nfl_signals.top_market(betting, standout_threshold)

    market_labels = ((config.get("insights_ui") or {}).get("nfl") or {}).get("market_labels") or {}
    raw_markets = nfl_signals.list_markets(betting)
    signal_scores = [{"market": market_labels.get(m["bet_type"], m["bet_type"]),
                      "side": m["side"], "score": m["score"]} for m in raw_markets]
    if standout:
        standout = {**standout, "market": market_labels.get(standout.get("bet_type"), standout.get("bet_type"))}

    probables = None
    if away_qb_name or home_qb_name:
        probables = {
            "away": {"name": away_qb_name} if away_qb_name else None,
            "home": {"name": home_qb_name} if home_qb_name else None,
        }

    return {
        "gamePk": g["game_id"],  # generic "id" field name, reused across sports -- see generate_insights._build_games_section
        "sport": "nfl",
        # nflverse's schedule has no in-progress state (it is only ever
        # "not yet played" or "final", updated once the game ends) -- unlike
        # MLB's Preview/Live/Final, there is no "Live" this data source can
        # report. A cron that runs mid-game will see "Preview" (blank score)
        # right up until the final is posted; see the PR description for why
        # that is a known, flagged limitation rather than something fixed
        # here.
        "status": "Final" if g.get("home_score") not in (None, "") else "Preview",
        "away": {"abbr": away, "name": away, "color": None},
        "home": {"abbr": home, "name": home, "color": None},
        "start": _format_kickoff(g.get("gameday"), g.get("gametime")),
        "venue": g.get("stadium"),
        "probables": probables,
        "signals": _display_signals(away, home, away_form, home_form),
        # No team_pulse.nfl config block exists (out of scope this pass --
        # see docs/leagues.md's branding/config-population steps for what a
        # full sport rollout still needs), so there is no deterministic
        # source for a game pulse the way MLB's OPS/bullpen-derived one has.
        # None renders without a Pulse rather than a fabricated placeholder.
        "pulse": None,
        "betting_signals": betting,
        "standout": standout,
        "best_angle": standout,
        "signal_scores": signal_scores,
        "compare": None,   # no insights_ui.nfl.compare_sets config -- degrades to no table, same as a missing sport block does today
        "est_total": None,  # no NFL total market yet (moneyline only, v1)
        "f5_total": None,
        "context": {
            "away_team": away, "home_team": home,
            "away_off_epa": away_form.get("off_epa"), "home_off_epa": home_form.get("off_epa"),
            "away_def_epa_allowed": away_form.get("def_epa_allowed"), "home_def_epa_allowed": home_form.get("def_epa_allowed"),
            "away_turnover_diff": away_form.get("turnover_diff"), "home_turnover_diff": home_form.get("turnover_diff"),
            "away_rest": g.get("away_rest"), "home_rest": g.get("home_rest"),
        },
    }


def build_game_entities(config, game_date, boxscore_cache, team_entities=None):
    """NFL's entry in generate_insights.GAME_BUILDERS -- same calling
    convention and return shape as fetchers.mlb.build_game_entities, scoped
    to what the moneyline market needs (see nfl_signals.py).

    `boxscore_cache` is accepted for interface parity but unused, and the
    second return value is always {}: NFL's signals come pre-aggregated from
    nflverse's team_stats release, so unlike MLB's 7-day bullpen ERA there is
    no per-game boxscore to reconstruct and cache across runs.

    `team_entities`, when a list is passed, is left untouched: there is no
    team_pulse.nfl config (see _build_one_game's docstring), so there is
    nothing deterministic to build a Team profile from yet. Accepting the
    parameter without using it keeps this a drop-in match for
    generate_insights._build_game_entities' calling convention.

    Returns (entities, {}, training_rows): entities keyed by nflverse's own
    `game_id` string (e.g. "2025_01_DAL_PHI"); training_rows is always []
    -- training_capture's store schema is MLB-specific (probable-pitcher
    features, MLB leakage gates) and out of scope here.

    Per-game failures are isolated exactly like fetchers/mlb.py's: one bad
    game is logged and skipped, the rest of the slate still builds.
    """
    session = requests.Session()
    games = get_games_on_date(session, game_date)
    if not games:
        return {}, {}, []

    season = season_for_date(game_date)
    schedule = get_schedule(session, season)
    team_stats = get_team_stats(session, season)
    injuries = get_injuries(session, season)

    entities = {}
    for g in games:
        try:
            entities[g["game_id"]] = _build_one_game(config, g, schedule, team_stats, injuries)
        except Exception as e:  # noqa: BLE001 -- one bad game must not cost the slate
            print("insights(games): nfl game {} ({} @ {}) failed to build ({}: {}); skipped"
                  .format(g.get("game_id"), g.get("away_team"), g.get("home_team"),
                          type(e).__name__, str(e)[:160]))

    return entities, {}, []
