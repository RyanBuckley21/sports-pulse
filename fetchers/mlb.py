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

import requests

REQUEST_TIMEOUT = 15


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


def build_candidate_pool(session, base_url, season, seed_categories, group, pool_size):
    """Union of season leaders across seed categories, deduped by player id."""
    pool = {}
    for category in seed_categories:
        for player in get_season_leaders(session, base_url, season, category, group, pool_size):
            if player["id"] is not None:
                pool[player["id"]] = player
    return pool


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


def fetch_rolling_sum_category(session, base_url, season, window_games, cat_cfg, pool_size, injured_ids, positions, playing_team_ids):
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], cat_cfg["group"], pool_size
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


def fetch_hit_streak_category(session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids):
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], "hitting", pool_size
    )
    records = []
    for person_id, player in pool.items():
        if person_id in injured_ids:
            continue
        if playing_team_ids and player.get("team_id") not in playing_team_ids:
            continue
        game_log = get_game_log(session, base_url, person_id, season, "hitting")
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
    session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, game_date
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

    if cat_cfg.get("starters_only"):
        pool = get_probable_starters(session, base_url, game_date)
        pool_is_starters = True
    else:
        pool = build_candidate_pool(session, base_url, season, cat_cfg["seed_leaderboards"], group, pool_size)
        pool_is_starters = False

    records = []
    for person_id, player in pool.items():
        if person_id in injured_ids:
            continue
        if not pool_is_starters and playing_team_ids and player.get("team_id") not in playing_team_ids:
            continue
        game_log = get_game_log(session, base_url, person_id, season, group)
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


def fetch(config, game_date=None):
    """Fetch raw (unranked) records for every configured MLB stat category."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    season = mlb_cfg["season"]
    default_window_games = mlb_cfg["window_games"]
    pool_size = mlb_cfg["candidate_pool_size"]
    game_date = game_date or datetime.date.today().isoformat()

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
                    session, base_url, season, window_games, cat_cfg, pool_size, injured_ids, positions, playing_team_ids
                )
            )
        elif cat_cfg["mode"] == "hit_streak":
            records.extend(
                fetch_hit_streak_category(session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids)
            )
        elif cat_cfg["mode"] == "threshold_rate":
            records.extend(
                fetch_threshold_rate_category(
                    session, base_url, season, cat_cfg, pool_size, injured_ids, positions, playing_team_ids, game_date
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
