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
import math

import pulse
import slate_clock

import requests

REQUEST_TIMEOUT = 20

# A plain git blob (not a Release asset) -- raw.githubusercontent.com serves
# it directly, one fetch covers every season on file.
NFLDATA_SCHEDULES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
# Release assets: one CSV per season, generated from that season's completed
# games -- see _get_csv's 404-as-[] handling for a season with none yet.
NFLVERSE_RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"

# HOW FAR AHEAD A SLATE REACHES. Same reason fetchers/cfb uses seven and
# fetchers/epl three: the NFL week runs Thursday to Monday, so a one-date slate
# leaves the Games tab empty most of the week. Measured on the real 2026
# opening week: Wed 0, Thu 1, Fri 0, Sat 0, Sun 13, Mon 1. Seven days always
# shows the week ahead, which is the slate a reader wants on a Tuesday.
FIXTURE_WINDOW_DAYS = 7

# Completed games a team needs THIS season before its own points margin is
# usable as a fallback signal. Five, and NOT the three CFB uses -- the number
# was measured for each sport separately. Walk-forward over 2002-2025, hit rate
# in weeks 1-5 at threshold 50: a floor of 3 hands off in week 4 and scores
# 63.7%; a floor of 5 hands off in week 6 and scores 65.1%, with weeks 6+
# unchanged at 71.4%. Week 6 is also where the measured crossover between last
# season's margin and this season's actually sits. See nfl_signals._FALLBACK_TIERS.
SEASON_MARGIN_MIN_GAMES = 5

# Games a team must have played LAST season before its margin is usable as the
# cold-start signal. Eight is half a season -- enough for a per-game average to
# mean something, and it excludes a team whose prior season was truncated in
# the source data rather than letting four games stand in for seventeen.
PRIOR_SEASON_MIN_GAMES = 8


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


def resolved_season(config):
    """The NFL season this run is about, from config if pinned and derived if
    not -- the SINGLE answer, used by fetch() and by generate_stats'
    `competition` label alike.

    `season` used to be a required config key, which is a standing annual trap:
    leave it at last year and every August the boards publish LAST season's
    leaders as if they were current, with nothing failing and nothing to notice.
    season_for_date already encodes nflverse's convention (a season is named for
    the year it starts; March-August resolves to the one about to begin), so the
    right value is derivable and does not need remembering.

    A pinned `season:` still wins, which is what a backtest or a replay needs.
    """
    pinned = (config.get("nfl") or {}).get("season")
    return pinned or season_for_date(slate_clock.eastern_date())


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


def build_scoring_margins(schedule_rows, upto_week, min_games=0):
    """Season-to-date average point differential per team, from completed
    (scored) games strictly before `upto_week` -- straight off the
    schedule's own away_score/home_score, independent of team_stats
    entirely (a team could in principle have a scoring-margin reading with
    no team_stats row at all, though in practice the two sources agree on
    which games are complete).

    `min_games` defaults to 0, the unfiltered behaviour this has always had and
    what `context` and nfl_backtest still want. The FALLBACK path passes
    SEASON_MARGIN_MIN_GAMES -- see nfl_signals.SIGNAL_SPECS for why the floored
    and unfloored versions are deliberately two different numbers."""
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
    return {team: round(sum(vals) / len(vals), 3) for team, vals in totals.items()
            if len(vals) >= min_games}


def build_prior_season_margin(prior_schedule_rows, min_games=PRIOR_SEASON_MIN_GAMES):
    """{team: last season's points margin per game}, regular season only.

    WHY: week 1 has no in-season data of any kind, and nflverse publishes a
    season's stats_team release only once that season has games -- so the whole
    of week 1 scored "No clear lean" every year, on the weekend the tab most
    needs to say something. This is the one real published number available
    before a snap.

    IT IS REAL BUT MODEST. Measured walk-forward over 2002-2025, the prior-season
    margin gap correlates with the eventual points margin at r=+0.27 in week 1
    (95% CI [+0.17, +0.36], 2,000 stdlib resamples, fixed seed), and picking the
    side it favours won 66.2% of week-1 games outright at threshold 50. That is
    well below the same measurement for college football (+0.46, 67.7%), which
    is what a far more compressed league should look like -- read it as "better
    than nothing and honestly scored", not as an edge.

    KEYLESS: the prior season's rows are already in the games.csv this fetcher
    fetches, so this costs no extra request at all.

    Reuses build_scoring_margins with no week ceiling -- "before week 999" is
    the whole regular season, and sharing it means the cold-start margin and the
    in-season one cannot drift on what counts as a game."""
    return build_scoring_margins(prior_schedule_rows, upto_week=999, min_games=min_games)


# --------------------------------------------------------------------------- #
# Teams tab -- built from the SCHEDULE alone.
# --------------------------------------------------------------------------- #

def build_schedule_form(schedule_rows, before_date, window=3):
    """Per-club form from COMPLETED SCHEDULE ROWS ALONE, mirroring
    fetchers/cfb.build_schedule_form.

    Deliberately separate from build_team_form, which is the nflverse
    team_stats/EPA path. This one needs no release asset at all -- games.csv is
    already fetched for the slate -- and it exists because the Teams tab needs
    a number on every run, including week 1 and every week before nflverse
    publishes that season's stats. A tab that renders only once a third party
    cuts a release is not a tab.

    It is NOT a substitute for the scored model and does not feed it: points
    scored and allowed are cruder than EPA, which is exactly why EPA carries the
    weights. Nothing here reaches nfl_signals.

    `before_date` of None means the whole (prior) season; a date means
    point-in-time, the same discipline as everywhere else in this repo.
    Regular season only -- a playoff run is a different competitive context and
    would flatter the teams that made it.
    """
    hist = {}
    for row in schedule_rows or []:
        if row.get("game_type") != "REG":
            continue
        date = row.get("gameday")
        if not date or (before_date and date >= before_date):
            continue
        hp, ap = _num(row.get("home_score")), _num(row.get("away_score"))
        if hp is None or ap is None:
            continue
        for team, pf, pa in ((row["home_team"], hp, ap), (row["away_team"], ap, hp)):
            hist.setdefault(team, []).append({"pf": pf, "pa": pa, "date": date, "won": pf > pa})
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
            "wins": sum(1 for p in past if p["won"]),
            "losses": sum(1 for p in past if not p["won"]),
            "form_string": "".join("W" if p["won"] else "L" for p in recent),
        }
    return form


def _nfl_team_pulse(form, cfg):
    """Deterministic 0-100 notability score for ONE club, the same shape
    fetchers/cfb._cfb_team_pulse and fetchers/mlb._team_pulse use: each
    component tanh-squashed against a league-average `base` by a `scale` that
    reads as a meaningful move, renormalized over whichever have data.

    Direction is intrinsic and lives here, not in config -- more points scored
    is good, FEWER allowed is good.

    Returns None below the sample floor. NFL's floor is the harshest in this
    repo relative to season length: three of seventeen games is a sixth of a
    season, and a single blowout moves a three-game average further here than
    anywhere else. A club with fewer gets no pulse rather than a number that
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
    total_w = sum(w for _, w in terms) or 1.0
    lean = sum(t * w for t, w in terms) / total_w
    return pulse.pulse(max(0, min(100, int(math.floor(50 + 50 * lean * 1.0 + 0.5)))))


def _stale_pulse(p, prior_season):
    """A pulse computed from LAST season, marked as such.

    A separate `qualifier` rather than a changed `label`, for the reason
    fetchers/cfb._stale_pulse records: insights.js keys the band COLOUR off the
    label word, so folding a season into it would silently grey out every one
    of these cards."""
    if not p or not prior_season:
        return p
    return {**p, "qualifier": "{} season".format(prior_season)}


def build_nfl_team_entities(config, form, slate_teams, prior_form=None, prior_season=None):
    """One Team card per club ON THIS SLATE, mirroring cfb/mlb/epl.

    `prior_form` is last season's form, used only for a club with nothing this
    season -- which in week 1 is every club, and would otherwise be an empty
    Teams tab through the opening month. Same fallback discipline the scored
    signals use: one window or the other, never blended, and every row built
    that way is LABELLED with the season it came from.
    """
    cfg = ((config.get("team_pulse") or {}).get("nfl") or {})
    if not cfg:
        return []
    prior_form = prior_form or {}
    out = []
    for team in slate_teams:
        f = form.get(team)
        stale = False
        if not f:
            f = prior_form.get(team)
            stale = bool(f)
        if not f:
            continue
        ref = _team_ref(team)
        suffix = " ({})".format(prior_season) if stale and prior_season else ""
        off_base = ((cfg.get("offense") or {}).get("base"))
        def_base = ((cfg.get("defense") or {}).get("base"))
        signals = [
            {"label": "Points scored / game" + suffix, "value": "%.1f" % f["pf_pg"],
             "tone": "pos" if (off_base is None or f["pf_pg"] >= off_base) else "neg"},
            {"label": "Points allowed / game" + suffix, "value": "%.1f" % f["pa_pg"],
             "tone": "pos" if (def_base is None or f["pa_pg"] <= def_base) else "neg"},
            {"label": "Record" + suffix, "value": "%d-%d" % (f["wins"], f["losses"]),
             "tone": "pos" if f["wins"] >= f["losses"] else "neg"},
        ]
        if f.get("form_string"):
            signals.append({"label": "Last %d" % len(f["form_string"]) + suffix,
                            "value": f["form_string"], "tone": "pos"})
        p = _nfl_team_pulse(f, cfg)
        out.append({
            "id": team, "sport": "nfl", "abbr": ref.get("abbr"), "name": ref.get("name") or team,
            "team_color": ref.get("color"),
            "pulse": _stale_pulse(p, prior_season) if stale else p,
            "signals": signals,
        })
    out.sort(key=lambda e: (-((e.get("pulse") or {}).get("score") or 0), e.get("abbr") or ""))
    return out


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


def _display_signals(away, home, away_form, home_form,
                     away_season_margin=None, home_season_margin=None,
                     away_prior_margin=None, home_prior_margin=None):
    """Team-relative framing chips, mirroring fetchers/mlb.py's `signals`
    list: surface the single more-notable side per family.

    The two margin rows are FALLBACKS IN THE SAME ORDER nfl_signals scores in,
    and only one of the three tiers is ever shown. A card listing "last season"
    beside live EPA would suggest the lean used both; it never does, and the
    card has to say which one it did use."""
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
    if not signals:
        # Both sides on one row, LABELLED WITH THE WINDOW IT CAME FROM -- the
        # whole point of these rows is that the reader can tell at a glance
        # whether the number is this season's or last year's.
        if away_season_margin is not None or home_season_margin is not None:
            signals.append(_margin_signal("Points margin / game", away, home,
                                          away_season_margin, home_season_margin))
        elif away_prior_margin is not None or home_prior_margin is not None:
            signals.append(_margin_signal("Last season margin / game", away, home,
                                          away_prior_margin, home_prior_margin))
    return signals


def _margin_signal(label, away, home, away_margin, home_margin):
    return {
        "label": label,
        "value": "{} {} \u00b7 {} {}".format(
            home, _fmt_margin(home_margin), away, _fmt_margin(away_margin)),
        "tone": "pos" if (home_margin or 0) >= (away_margin or 0) else "neg",
    }


def _fmt_margin(v):
    return "n/a" if v is None else "{:+.1f}".format(v)


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


def _build_one_game(config, g, schedule, team_stats, injuries, prior_margin=None):
    import nfl_signals

    away, home = g["away_team"], g["home_team"]
    week = int(g["week"])

    form = build_team_form(team_stats, week)
    margins = build_scoring_margins(schedule, week)
    season_margin = build_scoring_margins(schedule, week, min_games=SEASON_MARGIN_MIN_GAMES)
    prior_margin = prior_margin or {}
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
        # Fallback tiers, both keyless and both off the same games.csv already
        # in hand. nfl_signals drops them the moment any in-season signal
        # exists, so neither can dilute a calibrated lean -- see
        # nfl_signals._FALLBACK_TIERS.
        away_season_margin=season_margin.get(away), home_season_margin=season_margin.get(home),
        away_prior_margin=prior_margin.get(away), home_prior_margin=prior_margin.get(home),
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
        "signals": _display_signals(away, home, away_form, home_form,
                                    season_margin.get(away), season_margin.get(home),
                                    prior_margin.get(away), prior_margin.get(home)),
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
            "away_season_margin": season_margin.get(away), "home_season_margin": season_margin.get(home),
            "away_prior_margin": prior_margin.get(away), "home_prior_margin": prior_margin.get(home),
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
    season = season_for_date(game_date)
    all_rows = get_schedule(session)
    schedule = [r for r in all_rows if r.get("season") == str(season)]
    if not schedule:
        return {}, {}, []

    # A WINDOW, NOT ONE DATE -- see FIXTURE_WINDOW_DAYS. The NFL week runs
    # Thursday to Monday, so a single-date slate is empty most days.
    #
    # AND IT FALLS FORWARD WHEN THE WINDOW IS EMPTY. Seven days is right in
    # season -- any Tuesday catches Thursday, Sunday and Monday, the whole week
    # -- but it goes blank in a gap longer than itself, and NFL has exactly one:
    # the tail of the preseason. On the 2026 calendar the opener was nine days
    # out, so week 1 rendered as an empty tab for two days while its fixtures
    # sat in the schedule already, fully scoreable (their picks come from last
    # season's margin and cannot change between now and kickoff). See
    # slate_clock.window_start.
    start = slate_clock.window_start(
        [r.get("gameday") for r in schedule], game_date, FIXTURE_WINDOW_DAYS)
    window_end = (datetime.date.fromisoformat(start)
                  + datetime.timedelta(days=FIXTURE_WINDOW_DAYS)).isoformat()
    if start != game_date:
        print("insights(games): nfl no fixtures within {} days of {} -- showing the "
              "next slate instead ({} to {})".format(
                  FIXTURE_WINDOW_DAYS, game_date, start, window_end))
    games = [r for r in schedule
             if r.get("gameday") and start <= r["gameday"] <= window_end]
    if not games:
        return {}, {}, []

    team_stats = get_team_stats(session, season)
    injuries = get_injuries(session, season)

    # Last season's margins, for the games that have nothing else. Read from
    # the SAME games.csv already in hand -- no extra request -- and only when
    # some game on the slate actually lacks in-season form. A November slate
    # never touches this.
    prior_margin = {}
    if not team_stats:
        prior_rows = [r for r in all_rows if r.get("season") == str(season - 1)]
        prior_margin = build_prior_season_margin(prior_rows)
        if prior_margin:
            print("insights(games): nfl cold start -- no {} team stats published yet, so "
                  "{} teams carry a {} margin instead (see build_prior_season_margin)"
                  .format(season, len(prior_margin), season - 1))

    entities = {}
    for g in games:
        try:
            entities[g["game_id"]] = _build_one_game(
                config, g, schedule, team_stats, injuries, prior_margin)
        except Exception as e:  # noqa: BLE001 -- one bad game must not cost the slate
            print("insights(games): nfl game {} ({} @ {}) failed to build ({}: {}); skipped"
                  .format(g.get("game_id"), g.get("away_team"), g.get("home_team"),
                          type(e).__name__, str(e)[:160]))

    if team_entities is not None:
        # Built from the SCHEDULE's own scores, never from the team_stats
        # release -- so the Teams tab renders on every run, including week 1
        # and any week nflverse has not published yet.
        schools = []
        for g in games:
            for key in ("home_team", "away_team"):
                if g.get(key) and g[key] not in schools:
                    schools.append(g[key])
        prior_form = {}
        form = build_schedule_form(schedule, game_date)
        if any(t not in form for t in schools):
            prior_rows = [r for r in all_rows if r.get("season") == str(season - 1)]
            prior_form = build_schedule_form(prior_rows, None)
        team_entities.extend(build_nfl_team_entities(
            config, form, schools, prior_form, season - 1))

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
    season = season or resolved_season(config)
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
