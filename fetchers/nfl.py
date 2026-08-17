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
  * Team weekly stats, player weekly stats, and injuries -- nflverse-data's
    `stats_team`, `stats_player` and `injuries` GitHub Releases, one CSV per
    season each (`stats_team_week_{season}.csv`,
    `stats_player_week_{season}.csv`, `injuries_{season}.csv`). URL
    construction verified against nflreadr's own R source
    (github.com/nflverse/nflreadr/R/load_stats.R, load_injuries.R) and
    live-fetched against the real 2025 season while building this fetcher --
    also see the PR description for the exact columns confirmed.

Attribution: this data is nflverse's, released under CC BY 4.0. The site's
credits/footer needs a visible "NFL data via nflverse (CC BY 4.0)" line
before this fetcher's output ships to production -- not added here, since
that is a site-wide presentation change outside this module's job.

This module feeds TWO independent pipelines, and the split is deliberate --
they share these HTTP helpers and nothing else:

  * build_game_entities() -> generate_insights.GAME_BUILDERS. Scored Game
    entities for the MONEYLINE market: weights, thresholds, graded picks, the
    ledger. See nfl_signals.py.
  * fetch() -> generate_stats.SPORT_FETCHERS. The "Who's Hot" leaderboard:
    players ranked by raw production over a trailing window. No weights, no
    thresholds-as-conviction, nothing graded, nothing written to a ledger.

Neither calls into the other, and a change to one market's weights cannot
move a leaderboard (or vice versa). See the "Who's Hot" section header at the
bottom of this file for the full boundary note.

Still out of scope here: CFB, and every NFL bet type other than moneyline.
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


def _player_stats_url(season):
    """One row per (player, game) for a whole season -- the Who's Hot
    leaderboard's only data source. Same release/naming convention as the
    team file above, just the player summary level."""
    return "{}/stats_player/stats_player_week_{}.csv".format(NFLVERSE_RELEASES, season)


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


def get_player_stats(session, season):
    """Every (player, game) stat row for that season, or [] if the season has
    no completed games yet. One fetch (~8MB for a full season, ~19k rows)
    covers every player and every week -- see the Who's Hot section below for
    why that single bulk file removes the candidate-pool seeding MLB needs."""
    return _get_csv(session, _player_stats_url(season), required=False)


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


def _team_ref(abbr):
    """Branding for one club, following fetchers/cfb._team_ref and, behind it,
    fetchers/mlb._build_one_game's `teamref` -- the picks path's way of
    consuming team_meta, as distinct from generate_stats' leaderboard path.

    NFL_TEAMS is keyed by the SAME nflverse abbreviation every NFL row in this
    pipeline already carries, and its abbr value is that key, so the only
    field this actually changes is `color`. `name` deliberately stays the
    abbreviation: team_meta holds no full club name for the NFL (unlike MLB's
    table, which is keyed by name), and inventing one here would be a
    presentation change beyond closing the colour gap.

    Degrades to the previous behaviour for an unknown abbreviation -- abbr
    falls back to the input, colour stays None."""
    import team_meta  # local import, matching mlb.py's and cfb.py's convention
    meta = team_meta.get_team_meta("nfl", abbr) or {}
    return {"abbr": meta.get("abbr") or abbr, "name": abbr, "color": meta.get("color")}


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
        "away": _team_ref(away),
        "home": _team_ref(home),
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
            "away_scoring_margin": margins.get(away), "home_scoring_margin": margins.get(home),
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


# --------------------------------------------------------------------------- #
# "Who's Hot" leaderboard -- generate_stats.SPORT_FETCHERS path.
# --------------------------------------------------------------------------- #
#
# DELIBERATELY SEPARATE FROM EVERYTHING ABOVE. The section above builds scored
# Game entities for the moneyline market (nfl_signals.py, weights/thresholds,
# graded picks, the ledger); this section ranks players by raw production over
# a trailing window and stops there. No weights, no thresholds-as-conviction,
# no config from betting_signals, nothing graded, nothing written to a ledger.
# The two share this module and its HTTP helpers, and nothing else -- they are
# registered into two different registries (GAME_BUILDERS vs SPORT_FETCHERS)
# and neither calls into the other.
#
# fetchers/mlb.py's equivalent path carries a large candidate_pool_size /
# seed_leaderboards / recent_seed_leaderboards machinery. That exists ONLY
# because statsapi has no league-wide "last N games" leaderboard, so MLB has
# to seed a pool from season leaders and then re-query each player's rolling
# window one at a time. nflverse publishes every player-week of a season in a
# single CSV, so the exact window is computable for the entire league from one
# fetch. None of that seeding is reproduced here; copying it would be carrying
# a workaround for a constraint this data source does not have.


def _cat_num(row, field):
    """A stat field as a float, with blank/absent counted as 0.0.

    Distinct from the module-level `_num`, which returns None for a missing
    value so scoring inputs can tell "no data" from "zero". Here a blank IS a
    real zero: a player's row exists because they were active for that game,
    so an empty receiving_yards means they gained none, not that the figure is
    unknown."""
    return _num(row.get(field)) or 0.0


def _rows_by_player(player_stats, season):
    """{player_id: [rows]} for one season, each player's rows oldest-first.

    A row exists per game the player was actually active for -- verified
    against the real 2024 file: players carry between 1 and 17 regular-season
    rows, and a player who dressed but recorded nothing still has a row (all
    zeros). So "the last N rows" IS "the last N games they actually played",
    which is what makes byes and inactive weeks self-handling without any
    calendar arithmetic.

    Sorted by week, which orders postseason after regular season for free
    (nflverse numbers playoff weeks 19-22, continuing from the 18-week regular
    season) -- so a January board reads a player's genuinely most recent four
    games, whichever season type they fell in."""
    season_s = str(season)
    by_player = {}
    for row in player_stats:
        if row.get("season") != season_s:
            continue
        # Preseason is not published in this file at all (verified: the 2024
        # season_type values are only REG and POST), so there is nothing to
        # leak into a Week 1 window. Filtered explicitly anyway, so an upstream
        # schema change cannot quietly start counting exhibition production.
        if row.get("season_type") not in ("REG", "POST"):
            continue
        pid = row.get("player_id")
        if not pid:
            continue
        by_player.setdefault(pid, []).append(row)
    for rows in by_player.values():
        rows.sort(key=lambda r: int(r.get("week") or 0))
    return by_player


def _position_ok(row, positions, position_groups):
    """Whether a row survives a category's position filter. No filter
    configured means every position qualifies."""
    if positions and row.get("position") not in positions:
        return False
    if position_groups and row.get("position_group") not in position_groups:
        return False
    return True


def aggregate_category(by_player, cat_cfg, default_window, gameday_by_game_id):
    """Rank-ready raw records for ONE category, from the already-grouped
    per-player rows.

    The window is the player's most recent `window_games` rows -- games
    actually played, not calendar weeks (see _rows_by_player). `min_games`
    then drops anyone whose window is too thin to mean anything; without it a
    player returning from injury for one big game would top a four-game board
    off a single sample.

    `per_game: true` divides by the number of games IN THE PLAYER'S OWN
    WINDOW, not by window_games -- the same true-average rule
    fetchers/mlb.py's compute_category_value applies, so a player with 3 games
    in a 4-game window is averaged over 3.

    The position filter is applied HERE, at aggregation time, against the
    `position`/`position_group` already on every row -- no second fetch and no
    separate roster lookup. It is applied per ROW rather than once per player
    because position is a property of the row in this data; in practice a
    player's position is stable across a season, so this is equivalent to
    filtering the player, just without assuming it.
    """
    fields = cat_cfg["fields"]
    tiebreak_fields = cat_cfg.get("tiebreak_fields") or []
    window_games = cat_cfg.get("window_games", default_window)
    min_games = cat_cfg.get("min_games", 1)
    per_game = bool(cat_cfg.get("per_game"))
    positions = cat_cfg.get("positions")
    position_groups = cat_cfg.get("position_groups")

    records = []
    for pid, all_rows in by_player.items():
        rows = [r for r in all_rows if _position_ok(r, positions, position_groups)]
        window = rows[-window_games:]
        if len(window) < min_games:
            continue

        per_game_values = [sum(_cat_num(r, f) for f in fields) for r in window]
        total = sum(per_game_values)
        value = round(total / len(window), 2) if per_game else int(round(total))
        # Every window value being zero is not "hot" by any reading -- it is a
        # player who appeared and did nothing in this category (a WR with no
        # catches in four games, or any of the ~1,900 defenders whose rows
        # carry 0 for every offensive field). Dropping them here is what keeps
        # a low-volume board from filling its tail with zeroes.
        if total <= 0:
            continue

        latest = window[-1]
        records.append({
            "entity": latest.get("player_display_name") or latest.get("player_name"),
            "entity_id": pid,
            # Traded mid-season: the most recent row's team is the current one.
            "team": latest.get("team"),
            "team_id": None,
            "position": latest.get("position"),
            "stat_category": cat_cfg["key"],
            "window": "last_{}_games".format(window_games) + ("_per_game" if per_game else ""),
            "value": value,
            # How many games this player's window ACTUALLY spans, which is
            # <= window_games: early in a season nobody has four games yet,
            # and a bye or an inactive stretch leaves a returning player
            # short of the cap. Surfaced (rather than left implicit in
            # len(series)) so the rendered board label can state the real
            # depth instead of promising a four-game trend that does not
            # exist yet -- see generate_stats._resolve_sub.
            "games_window": len(window),
            # Small-integer boards (all four TD categories) pile up on ties --
            # a dozen players at 2 TDs is normal over four games. Ranking those
            # alphabetically by dict order would be arbitrary, so the
            # underlying yardage breaks it, reusing rank_records' existing
            # `tiebreak` key rather than adding a second sort mechanism.
            "tiebreak": sum(_cat_num(r, f) for r in window for f in tiebreak_fields) or None,
            "last_game_date": gameday_by_game_id.get(latest.get("game_id")),
            # The weekly rows ARE the per-game series, so it comes free here --
            # no post-ranking enrichment pass like fetchers/mlb.py needs. Same
            # as the World Cup fetcher, which also supplies its own series.
            "series": [
                {"date": gameday_by_game_id.get(r.get("game_id")), "value": v}
                for r, v in zip(window, per_game_values)
            ],
        })
    return records


def fetch(config, season=None):
    """Raw (unranked) Who's Hot records for every configured NFL stat
    category -- generate_stats.SPORT_FETCHERS' entry point for this sport.

    Two fetches total for the whole board set, regardless of how many
    categories are configured: the season's player-week file (every category
    aggregates from those same rows in memory) and the schedule (only to map
    game_id -> calendar date for `last_game_date` and the series labels).

    Returns [] when the season has no completed games yet -- get_player_stats
    passes required=False, so a 404 in the preseason is an empty board set
    rather than a failed build, and generate_stats simply emits no NFL section.
    """
    nfl_cfg = config["nfl"]
    season = season or nfl_cfg["season"]
    default_window = nfl_cfg.get("window_games", 4)

    session = requests.Session()
    player_stats = get_player_stats(session, season)
    if not player_stats:
        print("whos-hot(nfl): no player stats published for {} yet -- no boards".format(season))
        return []

    gameday_by_game_id = {g["game_id"]: g.get("gameday")
                          for g in get_schedule(session, season) if g.get("game_id")}
    by_player = _rows_by_player(player_stats, season)

    records = []
    for cat_cfg in nfl_cfg["stat_categories"]:
        if cat_cfg["mode"] != "rolling_sum":
            # v1 ships four per-game rate boards and four counting boards, all
            # rolling_sum. threshold_rate/streak modes exist in MLB's config
            # and would need their own branch here; raising keeps a typo or a
            # half-added category from silently producing an empty board.
            raise ValueError("Unknown NFL stat category mode: {}".format(cat_cfg["mode"]))
        records.extend(aggregate_category(by_player, cat_cfg, default_window, gameday_by_game_id))
    return records
