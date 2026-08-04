"""Fetcher for MLB data via the official (undocumented but stable) statsapi.mlb.com.

Strategy for rolling-window "who's hot" stats:
  1. Seed a candidate pool per stat category from the season-to-date league
     leaderboard (statsapi has no league-wide "last 10 games" leaderboard).
  2. Re-rank that pool using each player's actual rolling-window stats
     (lastXGames) or full-season game log (for hit streaks).

This keeps the number of API calls bounded (~1 + pool_size calls per
category) instead of pulling every boxscore in the window.
"""

import datetime
import math
import os

import requests

import pulse

REQUEST_TIMEOUT = 15


class SlateBuildError(RuntimeError):
    """Every game on a non-empty slate failed to build.

    Distinct from a single game failing (which build_game_entities skips and
    reports) and from an empty slate (a real off-day, which is {} and fine).
    Raised so callers can tell "there was nothing to build" apart from "the
    builder is broken" -- the two look identical from an empty return value,
    and treating the second as the first is how a full slate silently became
    no slate at all.
    """


def _annotate(title, detail):
    """Print `detail`, and under GitHub Actions also raise it to the run summary.

    Same convention as capture_training_data._fail: a degraded-but-successful
    run has to be diagnosable without opening the step logs, since nothing
    about the exit code says anything happened.
    """
    print("insights(games): {}: {}".format(title, detail))
    if os.environ.get("GITHUB_ACTIONS"):
        print("::warning title={}::{}".format(title, detail))


def _matchup_label(g):
    """"AWAY @ HOME" from a raw schedule game, for error messages only.

    Deliberately reads the raw payload rather than going through team_meta:
    this labels a game whose build just failed, so it cannot depend on any of
    the lookups that might be what failed.
    """
    teams = g.get("teams") or {}

    def side(key):
        t = (teams.get(key) or {}).get("team") or {}
        return t.get("abbreviation") or t.get("teamName") or t.get("name") or "?"

    return "{} @ {}".format(side("away"), side("home"))


def _get(session, url, params=None):
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_season_leaders(session, base_url, season, category, group, limit):
    """Season-to-date league leaders for one stat category. Used only to seed
    the candidate pool -- the season total itself is not the reported value."""
    data = _get(
        session,
        f"{base_url}/stats/leaders",
        params={
            "leaderCategories": category,
            "statGroup": group,
            "season": season,
            "sportId": 1,
            "limit": limit,
        },
    )
    leader_lists = data.get("leagueLeaders", [])
    if not leader_lists:
        return []
    leaders = []
    for entry in leader_lists[0].get("leaders", []):
        person = entry.get("person", {})
        team = entry.get("team", {})
        leaders.append(
            {
                "id": person.get("id"),
                "name": person.get("fullName"),
                "team": team.get("name"),
                "team_id": team.get("id"),
            }
        )
    return leaders


def get_recent_leaders(session, base_url, season, stat, start_date, end_date, group, limit):
    """League-wide leaders in `stat` over a DATE range (byDateRange endpoint),
    used to seed the candidate pool with recently-hot players the season boards
    miss (a rookie/light hitter on a heater ranks here but not in season totals).
    `/stats/leaders` ignores date params, so byDateRange is the only date-scoped
    path. Same output shape as get_season_leaders."""
    data = _get(
        session,
        f"{base_url}/stats",
        params={
            "stats": "byDateRange",
            "group": group,
            "season": season,
            "sportId": 1,
            "startDate": start_date,
            "endDate": end_date,
            "sortStat": stat,
            "limit": limit,
        },
    )
    leaders = []
    for block in data.get("stats", []):
        for split in block.get("splits", []):
            person = split.get("player", {})
            team = split.get("team", {})
            leaders.append(
                {
                    "id": person.get("id"),
                    "name": person.get("fullName"),
                    "team": team.get("name"),
                    "team_id": team.get("id"),
                }
            )
    return leaders


def build_candidate_pool(session, base_url, season, seed_categories, group, pool_size,
                         recent_seed_categories=None, recent_window=None,
                         recent_pool_size=None, recent_cache=None):
    """Union of season leaders across seed categories, deduped by player id.

    When `recent_seed_categories` + `recent_window` (start, end) are given, ALSO
    union the recent-window (byDateRange) leaders for those stats, so players who
    are hot over the last ~20 games but light on season totals (rookies,
    call-ups, light hitters on a heater) enter the pool and get re-ranked on
    their actual rolling window. `recent_cache` dedupes the recent-leader calls
    across the several categories that share the same window+stat."""
    pool = {}
    for category in seed_categories:
        for player in get_season_leaders(session, base_url, season, category, group, pool_size):
            if player["id"] is not None:
                pool[player["id"]] = player
    if recent_seed_categories and recent_window:
        start, end = recent_window
        limit = recent_pool_size or pool_size
        for stat in recent_seed_categories:
            key = (stat, group, start, end, limit)
            leaders = recent_cache.get(key) if recent_cache is not None else None
            if leaders is None:
                leaders = get_recent_leaders(session, base_url, season, stat, start, end, group, limit)
                if recent_cache is not None:
                    recent_cache[key] = leaders
            for player in leaders:
                if player["id"] is not None:
                    pool.setdefault(player["id"], player)  # season entry wins on dup
    return pool


def get_game_log_cached(session, base_url, person_id, season, group, cache, limit=None):
    """get_game_log with a per-run cache keyed by (person_id, group, limit). The
    hitting threshold + hit-streak categories share one candidate pool, so
    without this each re-walks the same players' full logs; caching collapses
    those to one fetch per player and pays for the recent-seed's extra players."""
    if cache is None:
        return get_game_log(session, base_url, person_id, season, group, limit=limit)
    key = (person_id, group, limit)
    if key not in cache:
        cache[key] = get_game_log(session, base_url, person_id, season, group, limit=limit)
    return cache[key]


def get_roster_index(session, base_url):
    """One pass over every team's 40-man roster, fetched once per run and
    reused across all categories, producing both:
      - injured: person ids on a Major League injured list (D7/D10/D15/D60
        roster status codes)
      - positions: person id -> position abbreviation (e.g. "3B", "SP"),
        for display in the player detail view
    Combined into one function since both are derived from the same roster
    calls -- splitting them would double the ~30 team-roster requests."""
    teams = _get(session, f"{base_url}/teams", params={"sportId": 1}).get("teams", [])
    injured = set()
    positions = {}
    for team in teams:
        roster = _get(
            session,
            f"{base_url}/teams/{team['id']}/roster",
            params={"rosterType": "40Man"},
        ).get("roster", [])
        for entry in roster:
            person_id = entry["person"]["id"]
            status_code = entry.get("status", {}).get("code", "")
            if status_code.startswith("D"):
                injured.add(person_id)
            abbr = entry.get("position", {}).get("abbreviation")
            if abbr:
                positions[person_id] = abbr
    return injured, positions


def get_teams_playing(session, base_url, date):
    """Team ids with a game scheduled on `date` (YYYY-MM-DD). The dashboard
    is a same-day "who's hot" board -- generated in the morning for that
    day's slate -- so every MLB category is restricted to players whose
    team actually takes the field, the same way strikeouts is already
    restricted to today's probable starters."""
    data = _get(session, f"{base_url}/schedule", params={"sportId": 1, "date": date})
    team_ids = set()
    for d in data.get("dates", []):
        for game in d.get("games", []):
            for side in ("away", "home"):
                team_id = game.get("teams", {}).get(side, {}).get("team", {}).get("id")
                if team_id is not None:
                    team_ids.add(team_id)
    return team_ids


def get_probable_starters(session, base_url, date):
    """Pitchers scheduled to start a game on `date` (YYYY-MM-DD), keyed by
    person id. Used to restrict the strikeouts category to today's starters
    instead of any pitcher who has appeared recently."""
    data = _get(
        session,
        f"{base_url}/schedule",
        params={"sportId": 1, "date": date, "hydrate": "probablePitcher"},
    )
    dates = data.get("dates", [])
    if not dates:
        return {}
    starters = {}
    for game in dates[0].get("games", []):
        for side in ("away", "home"):
            team_info = game.get("teams", {}).get(side, {})
            pitcher = team_info.get("probablePitcher")
            if not pitcher or pitcher.get("id") is None:
                continue
            starters[pitcher["id"]] = {
                "name": pitcher.get("fullName"),
                "team": team_info.get("team", {}).get("name"),
                "team_id": team_info.get("team", {}).get("id"),
            }
    return starters


def get_last_x_games_stat(session, base_url, person_id, season, group, window_games):
    """Rolling-window aggregate stats for one player, or None if they have no
    stats in the group this season (e.g. a position player with no pitching)."""
    data = _get(
        session,
        f"{base_url}/people/{person_id}/stats",
        params={
            "stats": "lastXGames",
            "limit": window_games,
            "group": group,
            "season": season,
        },
    )
    stats = data.get("stats", [])
    if not stats or not stats[0].get("splits"):
        return None
    split = stats[0]["splits"][0]
    return split.get("stat", {})


def get_game_log(session, base_url, person_id, season, group, limit=None):
    """Game-by-game log, oldest first, for players who appeared. Pass `limit`
    to fetch only the most recent N games (e.g. 1, just to get the last-played
    date cheaply) instead of the full season."""
    params = {"stats": "gameLog", "group": group, "season": season}
    if limit is not None:
        params["limit"] = limit
    data = _get(session, f"{base_url}/people/{person_id}/stats", params=params)
    stats = data.get("stats", [])
    if not stats:
        return []
    return stats[0].get("splits", [])


def get_last_game_date(session, base_url, person_id, season, group):
    game_log = get_game_log(session, base_url, person_id, season, group, limit=1)
    if not game_log:
        return None
    return game_log[-1].get("date")


def compute_hit_streak(game_log):
    """Consecutive most-recent games (from the end of the log) with >=1 hit."""
    streak = 0
    last_game_date = None
    for split in reversed(game_log):
        hits = split.get("stat", {}).get("hits", 0)
        if last_game_date is None:
            last_game_date = split.get("date")
        if hits and hits > 0:
            streak += 1
        else:
            break
    return streak, last_game_date


def compute_category_value(stat, cat_cfg):
    """Sum the configured fields; for `per_game` categories, divide by the
    player's *actual* games played in the window (from the API's own
    gamesPlayed count) rather than the requested window size, so a rookie
    with only 6 games this season -- or a window that otherwise doesn't
    line up with games actually played -- still gets a true per-game rate,
    not the sum spread over games they didn't play. Returns None if a
    per-game category has no games to divide by."""
    total = sum(stat.get(field, 0) or 0 for field in cat_cfg["fields"])
    if not cat_cfg.get("per_game"):
        return total
    games_played = stat.get("gamesPlayed") or 0
    if not games_played:
        return None
    return round(total / games_played, 2)


def fetch_rolling_sum_category(session, base_url, season, window_games, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, recent=None):
    recent = recent or {}
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], cat_cfg["group"], pool_size,
        recent_seed_categories=cat_cfg.get("recent_seed_leaderboards"),
        recent_window=recent.get("window"), recent_pool_size=recent.get("pool_size"),
        recent_cache=recent.get("cache"),
    )
    records = []
    for person_id, player in pool.items():
        if person_id in injured_ids:
            continue
        if playing_team_ids and player.get("team_id") not in playing_team_ids:
            continue
        stat = get_last_x_games_stat(session, base_url, person_id, season, cat_cfg["group"], window_games)
        if not stat:
            continue
        value = compute_category_value(stat, cat_cfg)
        if value is None:
            continue
        last_game_date = get_last_game_date(session, base_url, person_id, season, cat_cfg["group"])
        window = f"last_{window_games}_games" + ("_per_game" if cat_cfg.get("per_game") else "")
        records.append(
            {
                "entity": player["name"],
                "entity_id": person_id,
                "team": player["team"],
                "team_id": player.get("team_id"),
                "position": positions.get(person_id),
                "stat_category": cat_cfg["key"],
                "window": window,
                "value": value,
                "last_game_date": last_game_date,
            }
        )
    return records


def fetch_probable_starters_category(session, base_url, season, window_games, cat_cfg, injured_ids, positions, game_date):
    starters = get_probable_starters(session, base_url, game_date)
    records = []
    for person_id, player in starters.items():
        if person_id in injured_ids:
            continue
        stat = get_last_x_games_stat(session, base_url, person_id, season, cat_cfg["group"], window_games)
        if not stat:
            continue
        value = compute_category_value(stat, cat_cfg)
        if value is None:
            continue
        last_game_date = get_last_game_date(session, base_url, person_id, season, cat_cfg["group"])
        window = f"last_{window_games}_games_starters_only" + ("_per_game" if cat_cfg.get("per_game") else "")
        records.append(
            {
                "entity": player["name"],
                "entity_id": person_id,
                "team": player["team"],
                "team_id": player.get("team_id"),
                "position": positions.get(person_id),
                "stat_category": cat_cfg["key"],
                "window": window,
                "value": value,
                "last_game_date": last_game_date,
            }
        )
    return records


def fetch_hit_streak_category(session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, recent=None, game_log_cache=None):
    recent = recent or {}
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], "hitting", pool_size,
        recent_seed_categories=cat_cfg.get("recent_seed_leaderboards"),
        recent_window=recent.get("window"), recent_pool_size=recent.get("pool_size"),
        recent_cache=recent.get("cache"),
    )
    records = []
    for person_id, player in pool.items():
        if person_id in injured_ids:
            continue
        if playing_team_ids and player.get("team_id") not in playing_team_ids:
            continue
        game_log = get_game_log_cached(session, base_url, person_id, season, "hitting", game_log_cache)
        if not game_log:
            continue
        streak, last_game_date = compute_hit_streak(game_log)
        if streak <= 0:
            continue
        records.append(
            {
                "entity": player["name"],
                "entity_id": person_id,
                "team": player["team"],
                "team_id": player.get("team_id"),
                "position": positions.get(person_id),
                "stat_category": cat_cfg["key"],
                "window": "active_streak",
                "value": streak,
                "last_game_date": last_game_date,
            }
        )
    return records


def compute_threshold_rate(game_log, fields, threshold, window_games, starts_only=False):
    """How often a player cleared a per-game bar over their most recent
    window. Walks the (oldest-first) game log, counting games where the
    summed `fields` reach `threshold`. For pitchers, `starts_only` restricts
    the window to actual starts (gamesStarted >= 1) so relief outings don't
    count toward a "last 10 starts" window.

    Returns a dict with the rate plus a binary per-game series (1 = met,
    0 = missed, with the raw count kept for the bar label), or None if the
    player has no games in the window at all. The min-games qualification
    floor is applied by the caller, which knows the category's threshold."""
    splits = game_log
    if starts_only:
        splits = [s for s in splits if (s.get("stat", {}).get("gamesStarted") or 0) >= 1]
    window = splits[-window_games:]
    if not window:
        return None

    series = []
    met_count = 0
    for split in window:
        stat = split.get("stat", {})
        raw = int(sum(stat.get(f, 0) or 0 for f in fields))
        met = 1 if raw >= threshold else 0
        met_count += met
        series.append({"date": split.get("date"), "value": met, "raw": raw})

    return {
        "met": met_count,
        "window": len(window),
        "rate": round(met_count / len(window), 4),
        "series": series,
        "last_game_date": window[-1].get("date"),
    }


def fetch_threshold_rate_category(
    session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, game_date,
    recent=None, game_log_cache=None,
):
    """Rank players by how often they clear a per-game threshold within a
    recent window. Pool source mirrors the two rolling_sum paths: a
    `starters_only` category (K Rate) seeds from today's probable starters
    (so the same-day slate is implicit); everything else seeds from the
    season-leader candidate pool and applies the injured + same-day-team
    filters, exactly like fetch_hit_streak_category.

    The per-game log is walked here (same network cost profile as hit
    streak), so the binary series is built inline -- no separate post-rank
    enrichment pass is needed for these."""
    group = cat_cfg.get("group", "hitting")
    fields = cat_cfg["fields"]
    threshold = cat_cfg["threshold"]
    window_games = cat_cfg["window_games"]
    min_games = cat_cfg["min_games"]
    starts_only = bool(cat_cfg.get("window_starts_only"))

    recent = recent or {}
    if cat_cfg.get("starters_only"):
        pool = get_probable_starters(session, base_url, game_date)
        pool_is_starters = True
    else:
        pool = build_candidate_pool(
            session, base_url, season, cat_cfg["seed_leaderboards"], group, pool_size,
            recent_seed_categories=cat_cfg.get("recent_seed_leaderboards"),
            recent_window=recent.get("window"), recent_pool_size=recent.get("pool_size"),
            recent_cache=recent.get("cache"),
        )
        pool_is_starters = False

    records = []
    for person_id, player in pool.items():
        if person_id in injured_ids:
            continue
        if not pool_is_starters and playing_team_ids and player.get("team_id") not in playing_team_ids:
            continue
        game_log = get_game_log_cached(session, base_url, person_id, season, group, game_log_cache)
        if not game_log:
            continue
        result = compute_threshold_rate(game_log, fields, threshold, window_games, starts_only)
        if result is None or result["window"] < min_games:
            continue
        records.append(
            {
                "entity": player["name"],
                "entity_id": person_id,
                "team": player["team"],
                "team_id": player.get("team_id"),
                "position": positions.get(person_id),
                "stat_category": cat_cfg["key"],
                "window": f"threshold_last_{window_games}",
                "value": result["rate"],
                "tiebreak": result["met"],
                "met": result["met"],
                "games_window": result["window"],
                "series": result["series"],
                "last_game_date": result["last_game_date"],
            }
        )
    return records


def fetch_series_for_player(session, base_url, person_id, season, group, fields, window_games):
    """Per-game value series for one player, using the same `gameLog`
    endpoint already trusted for hit streaks -- just reading different
    fields out of each game's split instead of walking it for a streak.
    Pitching game logs also carry innings pitched per outing, kept as the
    raw API string ("5.2" is MLB thirds notation, 5 2/3 IP -- NOT a
    decimal; any future math on it must convert to outs first, never
    average the strings)."""
    game_log = get_game_log(session, base_url, person_id, season, group, limit=window_games)
    series = []
    for split in game_log:
        stat = split.get("stat", {})
        entry = {
            "date": split.get("date"),
            "value": int(sum(stat.get(f, 0) or 0 for f in fields)),
        }
        ip = stat.get("inningsPitched")
        if ip is not None:
            entry["ip"] = ip
        series.append(entry)
    return series


def enrich_with_series(ranked_records, config):
    """Attach a per-game series to each already-ranked MLB record, for the
    detail view's recent-form bars and breakdown stats. Deliberately runs
    *after* ranking/truncation so only the players who actually made a
    top-N board pay for the extra call -- not every member of the (much
    larger) candidate pool that was queried just to produce the rankings.
    Does not touch `value`/`rank`, which were already decided upstream."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    season = mlb_cfg["season"]
    default_window_games = mlb_cfg["window_games"]
    cat_cfg_by_key = {c["key"]: c for c in mlb_cfg["stat_categories"]}

    session = requests.Session()
    for r in ranked_records:
        cat_cfg = cat_cfg_by_key.get(r["stat_category"])
        if cat_cfg is None or r.get("entity_id") is None:
            continue
        # threshold_rate categories build their own binary series inline
        # during ranking -- don't clobber it with a magnitude series here.
        if cat_cfg["mode"] == "threshold_rate":
            continue
        window_games = cat_cfg.get("window_games", default_window_games)
        # Hit streaks are ranked off the full-season game log, not a fixed
        # window/fields config -- the recent-form bars for a streak show
        # hits over the same trailing window as every other category, so
        # fall back to that explicitly.
        group = cat_cfg.get("group", "hitting")
        fields = cat_cfg.get("fields") or ["hits"]
        r["series"] = fetch_series_for_player(session, base_url, r["entity_id"], season, group, fields, window_games)


def get_next_opposing_starter(session, base_url, team_id, from_date):
    """The announced probable starter this team will face in its next
    not-yet-finished game on/after `from_date`, or None if the next game
    has no announced opposing starter yet (statsapi simply omits the
    probablePitcher key until a starter is announced)."""
    end_date = (datetime.date.fromisoformat(from_date) + datetime.timedelta(days=7)).isoformat()
    data = _get(
        session,
        f"{base_url}/schedule",
        params={
            "sportId": 1,
            "teamId": team_id,
            "startDate": from_date,
            "endDate": end_date,
            "hydrate": "probablePitcher",
        },
    )
    for d in data.get("dates", []):
        for game in d.get("games", []):
            if game.get("status", {}).get("abstractGameState") == "Final":
                continue
            for side, other in (("away", "home"), ("home", "away")):
                if game.get("teams", {}).get(side, {}).get("team", {}).get("id") == team_id:
                    opp = game["teams"][other]
                    pitcher = opp.get("probablePitcher")
                    if not pitcher or pitcher.get("id") is None:
                        return None
                    return {
                        "pitcher_id": pitcher["id"],
                        "pitcher_name": pitcher.get("fullName"),
                        "pitcher_team": opp.get("team", {}).get("name"),
                        "game_date": game.get("officialDate"),
                    }
    return None


def get_vs_pitcher_career_line(session, base_url, batter_id, pitcher_id):
    """Career batter-vs-pitcher hitting line, or None if they've never
    faced each other (the vsPlayerTotal block comes back with an empty
    splits list in that case -- not zeros)."""
    data = _get(
        session,
        f"{base_url}/people/{batter_id}/stats",
        params={"stats": "vsPlayer", "opposingPlayerId": pitcher_id, "group": "hitting"},
    )
    for block in data.get("stats", []):
        if block.get("type", {}).get("displayName") != "vsPlayerTotal":
            continue
        splits = block.get("splits", [])
        if not splits:
            return None
        stat = splits[0].get("stat", {})
        return {
            "ab": int(stat.get("atBats") or 0),
            "hits": int(stat.get("hits") or 0),
            "hr": int(stat.get("homeRuns") or 0),
            "rbi": int(stat.get("rbi") or 0),
            "avg": stat.get("avg"),
        }
    return None


def enrich_with_vs_next_starter(ranked_records, config, game_date=None):
    """Attach each hitting-board player's career line against the probable
    starter their team faces next. Same top-N-only economics as the series
    enrichment, with two caches on top: one schedule lookup per team (not
    per record), one vsPlayer lookup per unique batter/pitcher pair (a
    batter on several boards pays once). Pitching boards (K/G) are skipped
    -- this is a batter-vs-pitcher stat. Records end up with
    `vs_next_starter` either as a merged dict (matchup + career line) or
    None when no starter is announced or there's no head-to-head history;
    nothing synthetic is ever filled in."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    game_date = game_date or datetime.date.today().isoformat()
    hitting_categories = {
        c["key"] for c in mlb_cfg["stat_categories"] if c.get("group", "hitting") == "hitting"
    }

    session = requests.Session()
    starter_by_team = {}
    line_by_pair = {}
    for r in ranked_records:
        if r["stat_category"] not in hitting_categories:
            continue
        batter_id, team_id = r.get("entity_id"), r.get("team_id")
        if batter_id is None or team_id is None:
            continue
        if team_id not in starter_by_team:
            starter_by_team[team_id] = get_next_opposing_starter(session, base_url, team_id, game_date)
        starter = starter_by_team[team_id]
        if not starter:
            r["vs_next_starter"] = None
            continue
        pair = (batter_id, starter["pitcher_id"])
        if pair not in line_by_pair:
            line_by_pair[pair] = get_vs_pitcher_career_line(session, base_url, batter_id, starter["pitcher_id"])
        line = line_by_pair[pair]
        if not line:
            r["vs_next_starter"] = None
            continue
        r["vs_next_starter"] = {
            "pitcher_name": starter["pitcher_name"],
            "pitcher_team": starter["pitcher_team"],
            "game_date": starter["game_date"],
            **line,
        }


# ---------------- Game insight entities (deterministic; "AI never calculates") ----------------
#
# One entity per game on a day's slate, assembled entirely from StatsAPI. The AI
# step (generate_insights.py) only writes prose from these numbers. See
# docs/sports-pulse-schema.md -> "Game signal catalog (v1)" for the contract and
# the team-relative framing rule surfaced here.


def _ip_to_outs(ip):
    """MLB thirds-notation innings-pitched string -> integer outs. "6.2" -> 20
    (6 innings + 2 outs), "1.0" -> 3, "" / None -> 0. NEVER treat the string as
    a decimal (see fetch_series_for_player's note)."""
    if not ip:
        return 0
    s = str(ip)
    whole, _, frac = s.partition(".")
    try:
        return int(whole) * 3 + (int(frac[0]) if frac else 0)
    except (ValueError, IndexError):
        return 0


def _recompute_ops(splits):
    """Sum hitting components across game splits and RECOMPUTE OPS = OBP + SLG
    (never average per-game OPS). Returns a rounded float, or None if no at-bats."""
    h = bb = hbp = ab = sf = tb = 0
    for sp in splits:
        st = sp.get("stat", {})
        h += int(st.get("hits", 0) or 0)
        bb += int(st.get("baseOnBalls", 0) or 0)
        hbp += int(st.get("hitByPitch", 0) or 0)
        ab += int(st.get("atBats", 0) or 0)
        sf += int(st.get("sacFlies", 0) or 0)
        tb += int(st.get("totalBases", 0) or 0)
    if ab == 0:
        return None
    obp_den = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_den if obp_den else 0.0
    slg = tb / ab
    return round(obp + slg, 3)


def _team_hitting_log(session, base_url, team_id, season, cache):
    """A team's full-season hitting game log splits, cached per team under a key
    shape distinct from the computed-OPS entries that share this dict.

    Lifted out of team_side_ops so the side-split and whole-team OPS variants can
    share ONE response. Both already filter client-side (the endpoint has no date
    or home/road parameter), so a team touched by both paths in the same run
    costs one HTTP call, not two."""
    key = ("log", team_id)
    if key in cache:
        return cache[key]
    data = _get(
        session,
        f"{base_url}/teams/{team_id}/stats",
        params={"stats": "gameLog", "group": "hitting", "season": season},
    )
    stats = data.get("stats", [])
    splits = stats[0].get("splits", []) if stats else []
    cache[key] = splits
    return splits


def _ops_window(splits, as_of_date, window_days):
    """The completed-game splits inside the trailing `window_days`, ending the day
    BEFORE as_of_date (today's game hasn't been played when the slate is built)."""
    cutoff = (datetime.date.fromisoformat(as_of_date) - datetime.timedelta(days=window_days)).isoformat()
    return [s for s in splits if s.get("date") and cutoff <= s["date"] < as_of_date]


def _ops_pa(splits):
    """Plate appearances behind an OPS window: the same AB+BB+HBP+SF denominator
    _recompute_ops already builds for OBP, returned instead of discarded so the
    rate can be weighted by the sample it came from."""
    return sum(int(sp.get("stat", {}).get(f, 0) or 0)
               for sp in splits for f in ("atBats", "baseOnBalls", "hitByPitch", "sacFlies"))


def team_side_ops(session, base_url, team_id, season, is_home, as_of_date, cache, window_days=14):
    """A team's recomputed OPS over its last `window_days` of completed games on
    one side (home games for the home team, road games for the away team). Cached
    per (team_id, is_home) so a doubleheader team isn't fetched twice. None if the
    team has no games on that side in the window."""
    key = (team_id, is_home)
    if key in cache:
        return cache[key]
    splits = _team_hitting_log(session, base_url, team_id, season, cache)
    windowed = [s for s in _ops_window(splits, as_of_date, window_days)
                if bool(s.get("isHome")) == is_home]
    ops = _recompute_ops(windowed)
    cache[key] = ops
    return ops


# --------------------------------------------------------------------------- #
# Stabilization (shrinkage toward the league baseline by sample size)
# --------------------------------------------------------------------------- #
#
# A rate stat off a thin sample is mostly noise, and until now a 14-innings
# bullpen ERA was handed to the pick model with exactly the same authority as a
# 36-innings one. Shrinkage fixes that with the standard empirical-Bayes form:
#
#     shrunk = league + (observed - league) * n / (n + k)
#
# k is the sample size at which the observed value earns half the weight. That
# is precisely the published definition of a "stabilization point" -- the point
# at which a stat "only needs to be regressed about halfway back to the mean"
# (FanGraphs' reliability work) -- so the sabermetric literature's stabilization
# numbers can be used as k directly.
#
# Every k below is now MEASURED against this repo's own data rather than borrowed
# from player-level literature. Each was derived by the attenuation identity
# r(observed, Y) = r(true, Y) * sqrt(reliability), taking r(true, Y) as the
# ceiling any team-level predictor can reach against single-game runs, then
# k = n * (1 - reliability) / reliability. Ceilings: 0.0754 for offence, 0.1353
# for run prevention (they are NOT the same -- teams differ about 3.2x more in
# run prevention than in run scoring).
#
#   OPS_STABILIZE_PA = 1855      r=+.0327 vs ceiling .0754 -> reliability .189
#   BULLPEN_STABILIZE_IP = 289   r=+.0361 vs ceiling .1353 -> reliability .071
#   STARTER_STABILIZE_IP = 141   r=+.0679 vs ceiling .1353 -> reliability .252
#
# Starter ERA is the STRONGEST of the three, not the weakest, and was previously
# used raw at any sample size even though its real sample runs from p10 8.7 IP to
# p90 95.3 IP.
#
# BULLPEN_SEASON_STABILIZE_IP = 1119 is the first stage of the bullpen's TWO-stage
# prior. A 7-day bullpen window is shrunk toward the team's OWN season-to-date
# bullpen ERA rather than toward the flat league mean, because measured directly
# the season-to-date value is the better predictor (r=+.0540 vs +.0361) and a
# blend test moved monotonically toward it. The season-to-date value is itself
# shrunk toward the league mean first, so a team with little season history still
# falls back to league average.
OPS_STABILIZE_PA = 1855
BULLPEN_STABILIZE_IP = 289
BULLPEN_SEASON_STABILIZE_IP = 1119
STARTER_STABILIZE_IP = 141


def shrink(observed, n, league, k):
    """Pull `observed` toward `league` by its own sample size: full weight in the
    limit, half weight at n == k, league mean at n == 0.

    Returns None when there is nothing to shrink (no observation), and the league
    baseline itself when the sample is empty but a baseline exists -- never a
    bare `observed` that quietly claims more precision than its sample supports.
    """
    if observed is None or league is None:
        return observed
    if not n:
        return round(float(league), 3)
    return round(float(league) + (float(observed) - float(league)) * (n / float(n + k)), 3)


def league_baseline(config, which):
    """The league mean to shrink toward: betting_signals.mlb.scales.<which>_solo.base.

    Read from the SCORING baselines rather than team_pulse's, because these are the
    numbers betting_signals itself centres its solo signals on -- shrinking an input
    toward a different constant than the scorer measures it against would put the
    "no information" case somewhere other than that scorer's zero point. The values
    are identical today (OPS .725, bullpen 4.00, starter ERA 4.00), so this is a
    provenance fix, not a behaviour change.

    `which` is one of "ops", "bullpen", "era". Note implied_total.LEAGUE_OPS is
    .720, close but not equal -- that one is a DIVISOR in a multiplicative run
    model (team_OPS / LEAGUE_OPS), not a centering constant, so the two play
    different mathematical roles and are not required to agree. Left alone here;
    implied_total is not this file's to edit.
    """
    scales = (((config or {}).get("betting_signals") or {}).get("mlb") or {}).get("scales") or {}
    return ((scales.get("{}_solo".format(which)) or {}).get("base"))


def team_season_bullpen_era(session, base_url, team_id, season, cache):
    """A team's SEASON-TO-DATE bullpen ERA as `(era, innings)`, for stage one of
    the bullpen prior. `(None, 0.0)` when unavailable.

    One API call per team, via the relief-pitcher season split, rather than
    re-deriving the season from boxscores: the GS=0 reconstruction would need every
    Final game of the season per team, and data/boxscores.json is pruned to the
    games each run touches (and is committed by no workflow), so that would mean
    ~1700 boxscore fetches on EVERY run rather than the ~100 the 7-day window costs
    today.

    The split's definition is role-based ("rp") where team_bullpen_era's is
    per-game (GS=0), so the two disagree slightly -- measured on NYY through
    2026-08-02, 3.01 over 398.1 IP here against 3.05 over 407.0 IP there, about
    1.3%. That gap is immaterial for a shrinkage TARGET, which only has to say
    roughly where this team's bullpen lives; the graded value is still the
    boxscore-derived one.
    """
    key = ("season_pen", team_id)
    if key in cache:
        return cache[key]
    era = ip = None
    try:
        data = _get(session, f"{base_url}/teams/{team_id}/stats",
                    params={"stats": "statSplits", "group": "pitching",
                            "season": season, "sitCodes": "rp"})
        stats = data.get("stats", [])
        if stats and stats[0].get("splits"):
            st = stats[0]["splits"][0].get("stat", {})
            era, ip = st.get("era"), st.get("inningsPitched")
    except requests.RequestException:
        era = ip = None
    try:
        out = (float(era), _ip_to_outs(ip) / 3.0)
    except (TypeError, ValueError):
        out = (None, 0.0)
    cache[key] = out
    return out


def bullpen_prior(config, pen7, ip7, pen_season, ip_season):
    """The two-stage bullpen value actually fed to the model.

        prior_team = league + (pen_season - league) * ip_season / (ip_season + 1119)
        used       = prior_team + (pen7 - prior_team) * ip7 / (ip7 + 289)

    Stage one pulls the team's season-to-date bullpen ERA toward the league mean by
    its own size; stage two pulls the 7-day window toward THAT team-specific value
    rather than toward the league mean. A team with no season history falls all the
    way back to league average, so the two-stage form degrades to the flat-mean one
    exactly where it should.
    """
    league = league_baseline(config, "bullpen")
    prior = shrink(pen_season, ip_season, league, BULLPEN_SEASON_STABILIZE_IP)
    if prior is None:
        prior = league
    if pen7 is None:
        return prior
    return shrink(pen7, ip7, prior, BULLPEN_STABILIZE_IP)


def team_window_ops(session, base_url, team_id, season, as_of_date, cache, window_days=14):
    """A team's recomputed OPS over its last `window_days` of completed games,
    HOME AND ROAD TOGETHER, as `(ops, plate_appearances)`. `(None, 0)` if it has
    no games in the window.

    The sample size is returned rather than discarded because the caller has to
    shrink the rate toward the league baseline by it -- see `shrink`. Callers
    that want the bare number must unpack; a tuple reaching arithmetic that
    expected a float raises rather than silently mis-scoring a pick.

    Deliberately not team_side_ops with a flag. The split version exists to frame
    one matchup -- the home team's home form against the away team's road form --
    and comparing those two directly carries a systematic pro-home tilt, since
    home splits run roughly .020-.030 OPS above road splits league-wide. A team
    profile has no opponent and no side to frame, so a split would not just
    inherit that tilt, it would answer a question nobody asked: half of a team's
    games would be silently discarded from its own form number.

    Shares the cache (and therefore the HTTP call) with team_side_ops via
    _team_hitting_log; the computed value is cached under (team_id, "all"), which
    cannot collide with the (team_id, bool) side entries."""
    key = (team_id, "all")
    if key in cache:
        return cache[key]
    splits = _team_hitting_log(session, base_url, team_id, season, cache)
    windowed = _ops_window(splits, as_of_date, window_days)
    out = (_recompute_ops(windowed), _ops_pa(windowed))
    cache[key] = out
    return out


def _bullpen_lines_from_boxscore(box):
    """Both teams' GS=0 (reliever) {er, ip_outs} from one boxscore, keyed by team
    id. Starters (gamesStarted>=1) are excluded -- this is a true bullpen line."""
    out = {}
    for side in ("away", "home"):
        tside = box.get("teams", {}).get(side, {})
        team_id = tside.get("team", {}).get("id")
        if team_id is None:
            continue
        er = outs = 0
        players = tside.get("players", {})
        for pid in tside.get("pitchers", []):
            pitch = players.get(f"ID{pid}", {}).get("stats", {}).get("pitching", {})
            if (pitch.get("gamesStarted") or 0) >= 1:
                continue
            er += int(pitch.get("earnedRuns", 0) or 0)
            outs += _ip_to_outs(pitch.get("inningsPitched"))
        out[str(team_id)] = {"er": er, "ip_outs": outs}
    return out


def team_bullpen_era(session, base_url, team_id, as_of_date, boxscore_cache, touched, window_days=7):
    """A team's true bullpen ERA over its Final games in the trailing
    `window_days`. Reuses the committed boxscore cache -- only Final gamePks NOT
    already cached are fetched; a cached (immutable) final game is never re-fetched.
    Every Final gamePk it considers is recorded in `touched` for cache pruning.

    Returns `(era, innings)`. `(None, 0.0)` if the bullpen threw no innings. The
    innings come back with the rate because a 7-day bullpen sample is the
    thinnest input this module produces -- a real median of about 24 innings, and
    as few as 14 -- so the caller has to shrink it toward the league baseline by
    its own size rather than trusting it flat. See `shrink`."""
    start = (datetime.date.fromisoformat(as_of_date) - datetime.timedelta(days=window_days)).isoformat()
    end = (datetime.date.fromisoformat(as_of_date) - datetime.timedelta(days=1)).isoformat()
    sched = _get(
        session,
        f"{base_url}/schedule",
        params={"sportId": 1, "teamId": team_id, "startDate": start, "endDate": end},
    )
    er = outs = 0
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            pk = str(g.get("gamePk"))
            touched.add(pk)
            entry = boxscore_cache.get(pk)
            if entry is None:
                entry = _bullpen_lines_from_boxscore(_get(session, f"{base_url}/game/{pk}/boxscore"))
                boxscore_cache[pk] = entry
            line = entry.get(str(team_id))
            if line:
                er += line["er"]
                outs += line["ip_outs"]
    if outs == 0:
        return None, 0.0
    innings = outs / 3.0
    return round(9.0 * er / innings, 2), round(innings, 1)


def season_series(session, base_url, team_id, opp_id, season, as_of_date):
    """This team's Win-Loss record vs one opponent among Final games this season
    through `as_of_date`. Returns (wins, losses) or None if they haven't met."""
    sched = _get(
        session,
        f"{base_url}/schedule",
        params={
            "sportId": 1, "teamId": team_id, "opponentId": opp_id,
            "startDate": f"{season}-01-01", "endDate": as_of_date,
        },
    )
    wins = losses = 0
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            for side in ("away", "home"):
                tinfo = g.get("teams", {}).get(side, {})
                if tinfo.get("team", {}).get("id") == team_id:
                    if tinfo.get("isWinner") is True:
                        wins += 1
                    elif tinfo.get("isWinner") is False:
                        losses += 1
    if wins + losses == 0:
        return None
    return (wins, losses)


def pitcher_season_era(session, base_url, pitcher_id, season, cache):
    """A pitcher's season ERA as `(raw API string, innings)` -- ("3.20", 84.0) --
    cached per id. `(None, 0.0)` if unavailable (best-effort; never fails the build).

    The innings come back with the rate for the same reason team_bullpen_era's do:
    the caller has to shrink the ERA toward the league baseline by its own sample,
    and a starter's season innings vary enormously across a slate (p10 8.7, p90
    95.3 measured over the 2026 season). A tuple reaching arithmetic that expected
    a bare ERA raises rather than silently mis-scoring a pick.
    """
    if pitcher_id in cache:
        return cache[pitcher_id]
    era = ip = None
    try:
        data = _get(
            session,
            f"{base_url}/people/{pitcher_id}/stats",
            params={"stats": "season", "group": "pitching", "season": season},
        )
        stats = data.get("stats", [])
        if stats and stats[0].get("splits"):
            st = stats[0]["splits"][0].get("stat", {})
            era, ip = st.get("era"), st.get("inningsPitched")
    except requests.RequestException:
        era = ip = None
    out = (era, _ip_to_outs(ip) / 3.0 if ip is not None else 0.0)
    cache[pitcher_id] = out
    return out


def _fmt_ops(v):
    """.812 style -- fixed 3 decimals, leading zero dropped."""
    if v is None:
        return None
    s = "{:.3f}".format(v)
    return s[1:] if s.startswith("0.") else s


def _num(v):
    """The API's numeric-as-string values ("3.20") as a float, or None. Used where
    a raw stat has to be arithmetic (shrunk) before it is formatted for display."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_era(v):
    """2-decimal ERA display from the raw API value, or None if not numeric."""
    try:
        return "{:.2f}".format(float(v))
    except (TypeError, ValueError):
        return None


def _format_start_et(game_date_utc):
    """UTC ISO gameDate -> 'H:MM AM/PM ET'. v1 fixes the display to US Eastern
    (a West Coast game shows ET, not local venue time -- a deliberate v1
    shortcut; real per-venue timezone can come later). None if unparseable."""
    if not game_date_utc:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        et = dt.astimezone(ZoneInfo("America/New_York"))
        hour = et.hour % 12 or 12
        return "{}:{:02d} {} ET".format(hour, et.minute, "AM" if et.hour < 12 else "PM")
    except Exception:  # noqa: BLE001 -- display nicety, never break the build
        return None


def _game_pulse(framed_ops, framed_bullpen_era, series):
    """Deterministic notability score for a game (games have no leaderboard rank).
    First-pass, tunable heuristic: a hot framed offense, a fatigued framed bullpen,
    and a lopsided season series each bump the score. Same band labels as players."""
    score = 55
    if framed_ops is not None:
        score += 15 if framed_ops >= 0.800 else 8 if framed_ops >= 0.750 else 0
    if framed_bullpen_era is not None:
        score += 12 if framed_bullpen_era >= 5.0 else 6 if framed_bullpen_era >= 4.25 else 0
    if series is not None:
        diff = abs(series[0] - series[1])
        score += 8 if diff >= 4 else 4 if diff >= 2 else 0
    # Both clamps are currently dead -- the base is 55 and every term is
    # additive, so the reachable range is 55..90 -- but they are the guard that
    # keeps this honest if a future term ever subtracts, so they stay.
    score = max(0, min(100, score))
    return pulse.pulse(score)


def _team_pulse(ops, bullpen_era, cfg):
    """Deterministic 0-100 notability score for ONE team from its own form.

    Each component is squashed against its league-average baseline with the same
    tanh shape betting_signals uses, so a big gap can't dominate: `base` is the
    league average and `scale` is the distance that reads as a meaningful move.
    Direction is intrinsic to the metric and lives here in code, not in config --
    higher OPS is good, LOWER bullpen ERA is good, hence the flipped numerator.

    The two components are renormalized over whichever ones actually have data.
    A team with no games in one window is scored on the other alone rather than
    being handed a substitute value or penalized for the gap; a team with neither
    gets None, and the card renders without a Pulse rather than showing a 50 that
    would look like a real measurement. 50 is the league-average score, not the
    no-data score.
    """
    terms = []
    ops_cfg = (cfg or {}).get("ops") or {}
    pen_cfg = (cfg or {}).get("bullpen") or {}
    if ops is not None and ops_cfg.get("scale"):
        terms.append((math.tanh((ops - ops_cfg["base"]) / ops_cfg["scale"]),
                      ops_cfg.get("weight", 0.5)))
    if bullpen_era is not None and pen_cfg.get("scale"):
        terms.append((math.tanh((pen_cfg["base"] - bullpen_era) / pen_cfg["scale"]),
                      pen_cfg.get("weight", 0.5)))
    if not terms:
        return None
    wsum = sum(w for _, w in terms)
    if wsum <= 0:
        return None
    combined = sum(d * w for d, w in terms) / wsum
    # Round half UP, matching betting_signals._round -- banker's rounding would
    # make a hand-checked score off by one at exact .5 boundaries.
    score = int(math.floor(50 + 50 * combined + 0.5))
    return pulse.pulse(max(0, min(100, score)))


def _label_markets(markets, labels):
    """Apply readable sport market labels to betting_signals.list_markets() rows.
    The label map is sport config (insights_ui.<sport>.market_labels); this keeps
    the UI's ranked Signal Score list free of raw snake_case bet_type keys."""
    return [{"market": labels.get(m["bet_type"], m["bet_type"]),
             "side": m["side"], "score": m["score"]} for m in markets]


def _est_fields(est):
    """The display subset of an estimate dict -- what actually rides along on a
    market row. `label`/`note` stay behind: the row shows "Est. N unit (low-high)"
    inline, and the not-a-line qualifier is carried by the Run Estimate card."""
    if not est:
        return None
    return {k: est[k] for k in ("point", "low", "high", "unit") if k in est}


def _attach_estimates(signal_scores, raw_markets, standout, est_total, team_ests, f5_total=None):
    """Give the total markets the actual estimated numbers behind their scores.

    A total row otherwise shows only a 0-100 Signal Score and an Over/Under
    lean with no magnitude, while the estimates are computed from the same
    inputs a few lines away. game_total takes the combined number, each
    team_total side takes that team's own, and first_five_total takes the
    separate starters-only estimate (a different model, not a scaled-down
    game_total -- see implied_total).

    Mutates in place. `raw_markets` is list_markets()' output, carried alongside
    `signal_scores` purely because _label_markets drops `bet_type` (it emits
    {market, side, score}); the two lists are 1:1 and same-ordered, so zip
    recovers each row's bet_type without widening the emitted shape.
    `standout` is the same dict object the entity exposes as `best_angle`, so
    attaching here covers both.

    `team_ests` is {abbr: estimate-or-None}. A team_total side is matched by the
    leading token of its `side` ("KC Under" -> "KC"), the same prefix rule
    sideColor() uses client-side. Anything with no estimate -- a None
    est_total, or one team missing an input while the other has it -- gets
    nothing attached at all: no key, no placeholder, rendering exactly as it
    does today.
    """
    game_fields = _est_fields(est_total)
    f5_fields = _est_fields(f5_total)
    team_fields = {abbr: _est_fields(e) for abbr, e in (team_ests or {}).items()}

    def fields_for(bet_type, side):
        if bet_type == "game_total":
            return game_fields
        if bet_type == "first_five_total":
            return f5_fields
        if bet_type == "team_total":
            return team_fields.get(str(side or "").split(" ")[0])
        return None

    for raw, row in zip(raw_markets, signal_scores):
        f = fields_for(raw.get("bet_type"), raw.get("side"))
        if f:
            row.update(f)
    if standout:
        f = fields_for(standout.get("bet_type"), standout.get("side"))
        if f:
            standout.update(f)


def _build_compare(probables, compare_sets):
    """Resolve a config compare_set ("compare N metrics between two entities")
    against this game's probable starters. Sport-specific field extraction lives
    HERE (Python build), so the JS component only ever sees resolved rows. Only
    rows whose data is in the pipeline render -- a missing metric (K%/BB%/WHIP
    this phase) or an unannounced starter drops the row/table (same empty-state
    discipline as the notes). Returns None when nothing usable remains."""
    probables = probables or {}
    ap, hp = probables.get("away") or {}, probables.get("home") or {}
    cs = (compare_sets or {}).get("probable_starters")
    if not cs or not (ap.get("name") and hp.get("name")):
        return None
    rows = []
    for metric in cs.get("metrics", []):
        key = metric.get("key")
        av, bv = ap.get(key), hp.get(key)  # e.g. era lives directly on the probable
        if av is None or bv is None:
            continue  # metric not fetched yet -> omit this row, keep the rest
        better = None
        try:
            fa, fb = float(av), float(bv)
            if fa != fb:
                better = "a" if ((fa < fb) == (metric.get("better") == "low")) else "b"
        except (TypeError, ValueError):
            better = None
        rows.append({"label": metric.get("label", key), "a": av, "b": bv, "better": better})
    if not rows:
        return None
    return {"set": "probable_starters", "title": cs.get("title", "Compare"),
            "a": {"name": ap.get("name")}, "b": {"name": hp.get("name")}, "rows": rows}


def _build_one_game(session, base_url, season, game_date, g, boxscore_cache, touched,
                    ops_cache, era_cache, config, injured_ids, training_rows=None):
    import team_meta  # local import: keeps the standalone `python3 fetchers/mlb.py` helper working
    import betting_signals
    import implied_total
    import training_capture

    teams = g.get("teams", {})
    away_t = teams.get("away", {}).get("team", {})
    home_t = teams.get("home", {}).get("team", {})
    away_id, home_id = away_t.get("id"), home_t.get("id")

    def teamref(t):
        meta = team_meta.get_team_meta("mlb", t.get("name")) or {}
        return {"abbr": meta.get("abbr") or t.get("abbreviation"),
                "name": t.get("teamName"), "color": meta.get("color")}

    away_ref, home_ref = teamref(away_t), teamref(home_t)

    # OPS: each team's whole 14d form, home and road together (recomputed).
    #
    # This used to be team_side_ops -- the home team's HOME split against the away
    # team's ROAD split. That framing reads well but the comparison is not fair in
    # either direction. team_window_ops' own docstring names the first problem:
    # home splits run roughly .020-.030 OPS above road splits league-wide, so
    # putting one against the other hands the home team a systematic head start
    # before any real form is measured. Measured over the 96 games captured in
    # data/training/mlb_features.jsonl, moving to the combined window lifts away
    # OPS by a mean +.029 and drops home OPS by -.020 -- a .049 relative swing.
    #
    # The second problem is worse and is about sample size, not bias. A 14-day
    # side split is a HALF sample: median 5 games, minimum 0, and 7% of the split
    # numbers in that same set rest on two games or fewer -- MIL's road OPS on
    # 2026-07-28 was .284 off a single game, against .748 combined. Three teams
    # had zero games on the relevant side and produced None, dropping the input
    # out of the pick entirely. The combined window never dipped below 9 games.
    #
    # Both fixes have to land on this one line, because all four consumers read
    # it: betting_signals.build_inputs below, implied_total's three estimates,
    # training_capture's feature row, and the displayed signal badge. Keeping them
    # consistent with each other matters more than tuning any one in isolation, so
    # nothing downstream is adjusted to compensate.
    home_ops_raw, home_ops_pa = team_window_ops(session, base_url, home_id, season, game_date, ops_cache) if home_id else (None, 0)
    away_ops_raw, away_ops_pa = team_window_ops(session, base_url, away_id, season, game_date, ops_cache) if away_id else (None, 0)

    # Bullpen ERA (7d): true bullpen, boxscore-cached.
    home_pen_raw, home_pen_ip = team_bullpen_era(session, base_url, home_id, game_date, boxscore_cache, touched) if home_id else (None, 0.0)
    away_pen_raw, away_pen_ip = team_bullpen_era(session, base_url, away_id, game_date, boxscore_cache, touched) if away_id else (None, 0.0)

    # Season-to-date bullpen ERA -- stage one of the two-stage bullpen prior below.
    home_pen_season, home_pen_season_ip = team_season_bullpen_era(session, base_url, home_id, season, ops_cache) if home_id else (None, 0.0)
    away_pen_season, away_pen_season_ip = team_season_bullpen_era(session, base_url, away_id, season, ops_cache) if away_id else (None, 0.0)

    # Stabilize every rate by its own sample size, HERE rather than in any one
    # consumer, so every downstream reader sees the same number:
    # betting_signals.build_inputs below, implied_total's three estimates,
    # training_capture's feature row, and the displayed badge. Doing it
    # per-consumer would leave them disagreeing about what a team's form is,
    # which is worse than any one of them being individually mis-scaled.
    #
    # OPS and starter ERA shrink toward the flat league baseline. The bullpen
    # does NOT -- it shrinks toward the team's own season-to-date value, which
    # measured directly is the better predictor of runs allowed than the 7-day
    # window is (r=+.0540 against +.0361). See bullpen_prior.
    _ops_base = league_baseline(config, "ops")
    _era_base = league_baseline(config, "era")
    home_ops = shrink(home_ops_raw, home_ops_pa, _ops_base, OPS_STABILIZE_PA)
    away_ops = shrink(away_ops_raw, away_ops_pa, _ops_base, OPS_STABILIZE_PA)
    home_pen = bullpen_prior(config, home_pen_raw, home_pen_ip, home_pen_season, home_pen_season_ip)
    away_pen = bullpen_prior(config, away_pen_raw, away_pen_ip, away_pen_season, away_pen_season_ip)

    # Season series (counts kept from the away team's perspective).
    series = season_series(session, base_url, away_id, home_id, season, game_date) if (away_id and home_id) else None

    # Probable starters + season ERA (best-effort; omit an unannounced side).
    # The ERA is shrunk toward the league baseline by the starter's own season
    # innings -- it is the strongest of the three measured inputs (reliability
    # .252) but was previously trusted flat at any sample size, including an
    # April call-up with a single start on file.
    away_pp = teams.get("away", {}).get("probablePitcher")
    home_pp = teams.get("home", {}).get("probablePitcher")
    probables = {}
    away_era = home_era = None
    away_era_ip = home_era_ip = 0.0
    if away_pp and away_pp.get("id"):
        _e, away_era_ip = pitcher_season_era(session, base_url, away_pp["id"], season, era_cache)
        away_era = _fmt_era(shrink(_num(_e), away_era_ip, _era_base, STARTER_STABILIZE_IP))
        probables["away"] = {"name": away_pp.get("fullName"), **({"era": away_era} if away_era else {})}
    if home_pp and home_pp.get("id"):
        _e, home_era_ip = pitcher_season_era(session, base_url, home_pp["id"], season, era_cache)
        home_era = _fmt_era(shrink(_num(_e), home_era_ip, _era_base, STARTER_STABILIZE_IP))
        probables["home"] = {"name": home_pp.get("fullName"), **({"era": home_era} if home_era else {})}

    # ---- Team-relative framing: surface the single most-notable side per
    # one-sided family; keep inherently-paired families combined. Both sides are
    # always preserved in `context` for the AI (display=standout, AI=complete).
    signals = []
    framed_ops = None
    if home_ops is not None or away_ops is not None:
        # No home/road qualifier: the number is now the team's whole 14-day window,
        # so naming a side would describe a split that is no longer being taken.
        if (away_ops or -1) > (home_ops or -1):
            framed_ops = away_ops
            signals.append({"label": f"{away_ref['abbr']} OPS (14d)", "value": _fmt_ops(away_ops), "tone": "pos"})
        else:
            framed_ops = home_ops
            signals.append({"label": f"{home_ref['abbr']} OPS (14d)", "value": _fmt_ops(home_ops), "tone": "pos"})

    framed_pen = None
    if home_pen is not None or away_pen is not None:
        # Explicit None checks, NOT an `x or -1` sentinel: 0.00 is a real bullpen
        # ERA (a short, clean week -- BAL and CWS both posted one in 2026), and
        # 0.0 is falsy, so the sentinel collapsed it onto the same value as "no
        # 7-day sample at all". When one side was 0.00 and the other None, both
        # sides of the comparison became -1, the `>` was False, and the else
        # branch formatted the None -- TypeError, which the caller's blanket
        # except then turned into a silently dropped slate.
        #
        # Framing rule is unchanged: lower is better, so the HIGHER ERA is the
        # side worth flagging. The only new behaviour is that when just one side
        # has a number, that side frames unconditionally -- there is nothing to
        # compare it against. Both-None is still handled by the guard above
        # (framed_pen stays None and no signal is appended).
        if away_pen is None or (home_pen is not None and home_pen > away_pen):
            framed_pen = home_pen
            signals.append({"label": f"{home_ref['abbr']} bullpen ERA (7d)", "value": "{:.2f}".format(home_pen), "tone": "neg"})
        else:
            framed_pen = away_pen
            signals.append({"label": f"{away_ref['abbr']} bullpen ERA (7d)", "value": "{:.2f}".format(away_pen), "tone": "neg"})

    if series is not None:
        w, l = series  # away team's W-L vs home
        if w >= l:
            signals.append({"label": "Season series", "value": f"{away_ref['abbr']} {w}-{l}", "tone": "neutral"})
        else:
            signals.append({"label": "Season series", "value": f"{home_ref['abbr']} {l}-{w}", "tone": "neutral"})

    if away_era and home_era:
        signals.append({"label": "Probables ERA", "value": f"{away_era} vs {home_era}", "tone": "neutral"})

    # Betting Signal Layer -- deterministic per-bet-type Signal Scores from the
    # same inputs (AI only explains them later, never invents; runs in CI).
    # Availability override: a probable starter on the IL (id in injured_ids)
    # materially changes the ML/first-five/total markets -- see betting_signals.
    away_out = bool(away_pp and away_pp.get("id") in injured_ids)
    home_out = bool(home_pp and home_pp.get("id") in injured_ids)
    signal_inputs = betting_signals.build_inputs(away_ref, home_ref, away_ops, home_ops,
                                                 away_pen, home_pen, away_era, home_era, series)
    availability = {"away_probable_out": away_out, "home_probable_out": home_out}
    betting = betting_signals.score_game(config, "mlb", signal_inputs, availability=availability)

    # Training-data capture (Phase 1): snapshot the SAME deterministic inputs for
    # EVERY game on the slate -- not just ones with a standout -- into the
    # append-only training store. Read-only against betting_signals: the dict
    # above is passed through verbatim. training_capture applies the pre-game
    # leakage gates and returns None (with a skip log) for any game already
    # under way. Guarded: capture must never break the game build.
    if training_rows is not None:
        try:
            row = training_capture.build_feature_row(
                g, signal_inputs, availability, game_date,
                samples={"away_ops_pa": away_ops_pa, "home_ops_pa": home_ops_pa,
                         "away_bullpen_ip": away_pen_ip, "home_bullpen_ip": home_pen_ip,
                         "away_bullpen_season_ip": away_pen_season_ip,
                         "home_bullpen_season_ip": home_pen_season_ip,
                         "away_starter_ip": away_era_ip, "home_starter_ip": home_era_ip})
            if row is not None:
                training_rows.append(row)
        except Exception as e:  # noqa: BLE001 -- capture is strictly additive
            print("training: feature row failed for gamePk {} ({})"
                  .format(g.get("gamePk"), str(e)[:120]))

    # The single most-notable market (deterministic; None if nothing clears the
    # standout bar). Drives the AI's one-sentence betting_note downstream.
    standout = betting_signals.top_market(
        betting, ((config.get("betting_signals") or {}).get("mlb") or {}).get("standout_threshold", 50))

    # Resolve the UI presentation structures here (sport-aware Python build) so
    # the JS components stay sport-blind: the ranked Signal Score list, the Best
    # Angle (standout + readable label, also reused by the AI note), and the
    # generic comparison table. Labels/metrics come from insights_ui config.
    ui_cfg = ((config.get("insights_ui") or {}).get("mlb") or {})
    market_labels = ui_cfg.get("market_labels") or {}
    # Kept alongside the labelled rows so _attach_est_total can recover each
    # row's bet_type (labelling drops it); the two lists stay 1:1 and ordered.
    raw_markets = betting_signals.list_markets(betting)
    signal_scores = _label_markets(raw_markets, market_labels)
    if standout:
        standout = {**standout,
                    "market": market_labels.get(standout.get("bet_type"), standout.get("bet_type"))}
    compare = _build_compare(probables, ui_cfg.get("compare_sets"))

    # Deterministic implied game-total estimate from the SAME inputs (14d OPS,
    # probable ERA, 7d bullpen ERA). No AI, no odds, no park/weather/lineup data.
    # A rough heuristic point + propagated +/-1sigma band -- NOT a market line and
    # never to be shown as one. None when any input is missing (unannounced SP).
    # Unit/label wording comes from sport config (insights_ui), never hardcoded.
    _est_cfg = ((config.get("insights_ui") or {}).get("mlb") or {}).get("est_total") or {}
    est_total = implied_total.estimate(
        away_ops, home_ops, away_era, home_era, away_pen, home_pen,
        unit=_est_cfg.get("unit", "runs"), note=_est_cfg.get("note", implied_total.NOTE),
        label=_est_cfg.get("label", "Estimate"))
    # Per-team estimates for the Team Total market -- each side is its own bet,
    # so each gets its own number rather than a share of the combined total.
    # Same matchup pairing betting_signals uses for team_total (a team's offense
    # against the OPPONENT's staff), but note the argument order differs:
    # team_estimate takes (ops, opp_STARTER, opp_BULLPEN) while _team_total
    # takes (ops, opp_bullpen, opp_starter). Unit/note reuse the est_total
    # config; the label comes from the configured market label.
    _team_est_label = market_labels.get("team_total", "Team Total")
    team_ests = {
        away_ref["abbr"]: implied_total.team_estimate(
            away_ops, home_era, home_pen, unit=_est_cfg.get("unit", "runs"),
            note=_est_cfg.get("note", implied_total.NOTE), label=_team_est_label),
        home_ref["abbr"]: implied_total.team_estimate(
            home_ops, away_era, away_pen, unit=_est_cfg.get("unit", "runs"),
            note=_est_cfg.get("note", implied_total.NOTE), label=_team_est_label),
    }
    # First five innings: its own starters-only model with its own wording, not
    # a scaled-down game total. Needs only the two starter ERAs, so it can
    # survive a missing bullpen number that would sink est_total -- and dies on
    # an unannounced starter that est_total might survive.
    _f5_cfg = ((config.get("insights_ui") or {}).get("mlb") or {}).get("first_five_total") or {}
    f5_total = implied_total.first_five_estimate(
        away_ops, home_ops, away_era, home_era,
        unit=_f5_cfg.get("unit", "runs"), note=_f5_cfg.get("note", implied_total.NOTE),
        label=_f5_cfg.get("label", "First Five Estimate"))
    # Connect each estimate to the market it actually describes.
    _attach_estimates(signal_scores, raw_markets, standout, est_total, team_ests, f5_total)

    return {
        "gamePk": g.get("gamePk"),
        "sport": "mlb",
        "status": g.get("status", {}).get("abstractGameState"),
        "away": away_ref,
        "home": home_ref,
        "start": _format_start_et(g.get("gameDate")),
        "venue": (g.get("venue") or {}).get("name"),
        "probables": probables or None,
        "signals": signals,
        "pulse": _game_pulse(framed_ops, framed_pen, series),
        "betting_signals": betting,
        "standout": standout,
        "best_angle": standout,       # the standout, semantic name for the UI
        "signal_scores": signal_scores,
        "compare": compare,
        "est_total": est_total,
        # The first-five estimate is a TOP-LEVEL key, not a market-row attachment
        # like game_total's and team_total's. Those still ride on their own rows
        # via _attach_estimates, which is correct -- they are still scored markets.
        # first_five_total is not, so there is no row to hang this on; without a
        # key of its own the number was computed every run and silently discarded.
        "f5_total": f5_total,
        # Full both-sides context for the AI payload only -- never shown directly.
        "context": {
            "away_team": away_ref["name"], "home_team": home_ref["name"],
            "away_road_ops_14d": _fmt_ops(away_ops),
            "home_home_ops_14d": _fmt_ops(home_ops),
            "away_bullpen_era_7d": "{:.2f}".format(away_pen) if away_pen is not None else None,
            "home_bullpen_era_7d": "{:.2f}".format(home_pen) if home_pen is not None else None,
            "season_series": (f"{away_ref['abbr']} {series[0]}-{series[1]}" if series else None),
            "away_probable_era": away_era,
            "home_probable_era": home_era,
        },
    }


def _build_team_entities(session, base_url, season, game_date, games, config,
                         ops_cache, boxscore_cache, touched):
    """One deterministic Team entity per team on `game_date`'s slate.

    Built inside build_game_entities' run so it reuses that run's caches: the
    whole-team OPS shares each team's already-fetched hitting log (see
    team_window_ops), and the 7d bullpen ERA reuses the boxscore cache the game
    cards already populated. A team appearing twice (split doubleheader) is
    profiled once.

    v1 is deliberately smaller than the deferred mock it replaces: two numeric
    signals and a Pulse, no headline and no summary. Those were AI prose in the
    mock, and there is no deterministic source for them -- inventing placeholder
    text to fill the shape would be exactly the fabrication the rest of this
    pipeline refuses. The card renders without them.
    """
    import team_meta  # local import, matching _build_one_game's convention

    cfg = ((config.get("team_pulse") or {}).get("mlb") or {})
    if not cfg:
        return {}
    ops_days = (cfg.get("ops") or {}).get("window_days", 14)
    pen_days = (cfg.get("bullpen") or {}).get("window_days", 7)
    ops_base = (cfg.get("ops") or {}).get("base")
    pen_base = (cfg.get("bullpen") or {}).get("base")

    seen, out = set(), []
    for g in games:
        for side in ("away", "home"):
            t = ((g.get("teams") or {}).get(side) or {}).get("team") or {}
            team_id, full_name = t.get("id"), t.get("name")
            if team_id is None or team_id in seen:
                continue
            seen.add(team_id)
            meta = team_meta.get_team_meta("mlb", full_name) or {}
            # Shrunk on the same terms as the game builder above. A team's form
            # number must not differ between the Teams view and the game card
            # that quotes it, so the stabilization applies to both or neither.
            ops_raw, ops_pa = team_window_ops(session, base_url, team_id, season, game_date,
                                              ops_cache, window_days=ops_days)
            pen_raw, pen_ip = team_bullpen_era(session, base_url, team_id, game_date,
                                               boxscore_cache, touched, window_days=pen_days)
            pen_season, pen_season_ip = team_season_bullpen_era(session, base_url, team_id,
                                                                season, ops_cache)
            ops = shrink(ops_raw, ops_pa, ops_base, OPS_STABILIZE_PA)
            pen = bullpen_prior(config, pen_raw, pen_ip, pen_season, pen_season_ip)
            abbr = meta.get("abbr") or t.get("abbreviation")
            # Tone is the CONNOTATION, not the raw direction (the convention
            # keySignals documents): a bullpen ERA better than league average
            # reads "pos" even though the number is low. The framed game signals
            # hardcode their tone because framing has already picked the notable
            # side; a standalone team has no such framing, so tone is measured
            # against the same league baseline the Pulse uses.
            signals = []
            if pen is not None:
                signals.append({
                    "label": "{} bullpen ERA ({}d)".format(abbr, pen_days),
                    "value": "{:.2f}".format(pen),
                    "tone": "pos" if (pen_base is not None and pen <= pen_base) else "neg",
                })
            if ops is not None:
                signals.append({
                    "label": "{} OPS ({}d)".format(abbr, ops_days),
                    "value": _fmt_ops(ops),
                    "tone": "pos" if (ops_base is not None and ops >= ops_base) else "neg",
                })
            out.append({
                "id": team_id,
                "sport": "mlb",
                "abbr": abbr,
                "name": t.get("teamName"),
                # `team_color`, NOT `color`: the pipeline's own key. The deferred
                # mock used `color`, and that mismatch is a known open item --
                # teamTag reads both so game TeamRefs keep working.
                "team_color": meta.get("color"),
                "pulse": _team_pulse(ops, pen, cfg),
                "signals": signals,
            })
    # Most notable first; abbr as a deterministic tiebreak so equal scores don't
    # reorder between runs. A team with no pulse at all sorts to the bottom.
    out.sort(key=lambda e: (-((e.get("pulse") or {}).get("score") or 0), e.get("abbr") or ""))
    return out


def build_game_entities(config, game_date, boxscore_cache, team_entities=None):
    """Build one deterministic Game insight entity for every game on `game_date`'s
    MLB slate (full slate -- uncapped, since "today's games" is already bounded).

    `team_entities`, when a list is passed, is populated in place with one Team
    entity per team on the slate -- the same out-parameter convention
    `training_rows` uses on _build_one_game. Left as None (the default) no team
    work is done at all, so callers that only want games pay nothing for this.

    Returns (entities, pruned_boxscore_cache, training_rows):
      - entities: ordered {str(gamePk): entity} in slate order; each entity carries
        away/home TeamRefs (abbr/name/color), start (ET), venue, probables, framed
        signals, a deterministic pulse, status, and a both-sides `context` block.
      - pruned_boxscore_cache: the input cache plus any newly fetched Final-game
        reliever lines, pruned to just the gamePks referenced this run (games that
        fall out of every team's 7d window drop off, keeping the file tiny).
      - training_rows: pre-game feature snapshots for the append-only training
        store (Phase 1), one per game that passed training_capture's leakage
        gates. Returned rather than written here so store I/O stays with the
        caller, matching how the boxscore cache is handled. Callers that don't
        want them can ignore the third element.

    Per-game failures are contained: a game that raises is logged with its
    gamePk, skipped, and reported through _annotate, and the rest of the slate
    is returned as normal. If EVERY scheduled game fails, that is the builder
    being broken rather than a bad game, and SlateBuildError is raised -- an
    empty return is reserved for a genuinely empty slate."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    season = mlb_cfg["season"]
    session = requests.Session()

    sched = _get(
        session,
        f"{base_url}/schedule",
        params={"sportId": 1, "date": game_date, "hydrate": "probablePitcher,team,venue"},
    )
    # Availability (IL) set for the betting layer's probable-starter override --
    # one league-wide roster pass (reuses get_roster_index -> D* status codes).
    # Guarded: any failure degrades to "no overrides", never breaks the build.
    try:
        injured_ids, _positions = get_roster_index(session, base_url)
    except Exception as e:  # noqa: BLE001 -- availability is best-effort
        print("insights(games): roster/availability pass failed ({}); no IL overrides"
              .format(str(e)[:120]))
        injured_ids = set()
    touched = set()
    ops_cache, era_cache = {}, {}
    entities = {}
    training_rows = []
    scheduled = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    failed = []
    for g in scheduled:
        pk = str(g.get("gamePk"))
        try:
            entities[pk] = _build_one_game(
                session, base_url, season, game_date, g, boxscore_cache, touched,
                ops_cache, era_cache, config, injured_ids, training_rows=training_rows,
            )
        except Exception as e:  # noqa: BLE001 -- one bad game must not cost the slate
            # THIS is the isolation the caller's guard was believed to provide.
            # It never did: the loop was bare, so a single game raising here
            # propagated out of build_game_entities and the caller turned the
            # WHOLE slate into {}. Skipping the one game keeps the other N-1
            # games, their signals, and their training rows.
            failed.append(pk)
            print("insights(games): game {} ({}) failed to build ({}: {}); skipped"
                  .format(pk, _matchup_label(g), type(e).__name__, str(e)[:160]))

    # Every game failing is not a partial slate -- it is the build being broken
    # (schema change, bad config, an endpoint returning something new), and it
    # must reach the caller as a failure rather than as an empty result. An
    # empty slate with nothing scheduled is a legitimate off-day and returns {}.
    # Checked BEFORE the partial-slate annotation below, which would otherwise
    # promise that "the rest of the slate is unaffected" when there is no rest.
    if scheduled and not entities:
        raise SlateBuildError(
            "all {} games on the {} slate failed to build; see the per-game "
            "errors above".format(len(scheduled), game_date))

    # A partial slate is survivable but never silent -- under Actions the count
    # lands on the run summary, so a quietly shrinking slate is visible without
    # opening the logs.
    if failed:
        _annotate("Games missing from the {} slate".format(game_date),
                  "{} of {} games failed to build and were skipped ({}). The rest of "
                  "the slate, its signals and its training rows are unaffected."
                  .format(len(failed), len(scheduled), ", ".join(failed)))

    # Teams, from the same session and the same caches the game loop just warmed.
    # Guarded separately: the Teams view is additive, and a failure here must not
    # cost the slate its games, signals, or training rows.
    if team_entities is not None:
        try:
            all_games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
            team_entities.extend(_build_team_entities(
                session, base_url, season, game_date, all_games, config,
                ops_cache, boxscore_cache, touched))
        except Exception as e:  # noqa: BLE001 -- teams are additive
            print("insights(teams): builder failed ({}); teams section skipped"
                  .format(str(e)[:160]))

    pruned_cache = {pk: boxscore_cache[pk] for pk in touched if pk in boxscore_cache}
    return entities, pruned_cache, training_rows


def fetch(config, game_date=None):
    """Fetch raw (unranked) records for every configured MLB stat category."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    season = mlb_cfg["season"]
    default_window_games = mlb_cfg["window_games"]
    pool_size = mlb_cfg["candidate_pool_size"]
    game_date = game_date or datetime.date.today().isoformat()

    # Recent-form pool seed: a date window (~20 games) whose byDateRange leaders
    # are unioned into the season pool so recently-hot low-season-volume hitters
    # get evaluated. `cache` dedupes recent-leader calls across the hitting
    # categories that share the same window+stat; game_log_cache collapses their
    # shared full-log fetches to one per player.
    recent_days = mlb_cfg.get("recent_seed_days", 24)
    recent_start = (datetime.date.fromisoformat(game_date) - datetime.timedelta(days=recent_days)).isoformat()
    recent = {"window": (recent_start, game_date),
              "pool_size": mlb_cfg.get("recent_pool_size", pool_size),
              "cache": {}}
    game_log_cache = {}

    session = requests.Session()
    injured_ids, positions = get_roster_index(session, base_url)
    # Empty on an off-day (no games scheduled): the category fetchers treat
    # an empty set as "don't filter" so a rare no-game day still shows the
    # latest boards instead of an empty dashboard.
    playing_team_ids = get_teams_playing(session, base_url, game_date)
    records = []
    for cat_cfg in mlb_cfg["stat_categories"]:
        window_games = cat_cfg.get("window_games", default_window_games)
        if cat_cfg["mode"] == "rolling_sum" and cat_cfg.get("starters_only"):
            records.extend(
                fetch_probable_starters_category(
                    session, base_url, season, window_games, cat_cfg, injured_ids, positions, game_date
                )
            )
        elif cat_cfg["mode"] == "rolling_sum":
            records.extend(
                fetch_rolling_sum_category(
                    session, base_url, season, window_games, cat_cfg, pool_size, injured_ids, positions, playing_team_ids,
                    recent=recent,
                )
            )
        elif cat_cfg["mode"] == "hit_streak":
            records.extend(
                fetch_hit_streak_category(
                    session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids,
                    recent=recent, game_log_cache=game_log_cache,
                )
            )
        elif cat_cfg["mode"] == "threshold_rate":
            records.extend(
                fetch_threshold_rate_category(
                    session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, game_date,
                    recent=recent, game_log_cache=game_log_cache,
                )
            )
        else:
            raise ValueError(f"Unknown MLB stat category mode: {cat_cfg['mode']}")
    return records


if __name__ == "__main__":
    import yaml

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    results = fetch(cfg)
    print(f"Fetched {len(results)} raw MLB records")
    for r in results[:5]:
        print(r)
