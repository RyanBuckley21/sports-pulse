"""Fetcher for MLB data via the official (undocumented but stable) statsapi.mlb.com.

Strategy for rolling-window "who's hot" stats:
  1. Seed a candidate pool per stat category from the season-to-date league
     leaderboard (statsapi has no league-wide "last 10 games" leaderboard).
  2. Re-rank that pool using each player's actual rolling-window stats
     (lastXGames) or full-season game log (for hit streaks).

This keeps the number of API calls bounded (~1 + pool_size calls per
category) instead of pulling every boxscore in the window.
"""

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


def fetch_rolling_sum_category(session, base_url, season, window_games, cat_cfg, pool_size):
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], cat_cfg["group"], pool_size
    )
    records = []
    for person_id, player in pool.items():
        stat = get_last_x_games_stat(session, base_url, person_id, season, cat_cfg["group"], window_games)
        if not stat:
            continue
        value = sum(stat.get(field, 0) or 0 for field in cat_cfg["fields"])
        last_game_date = get_last_game_date(session, base_url, person_id, season, cat_cfg["group"])
        records.append(
            {
                "entity": player["name"],
                "team": player["team"],
                "stat_category": cat_cfg["key"],
                "window": f"last_{window_games}_games",
                "value": value,
                "last_game_date": last_game_date,
            }
        )
    return records


def fetch_hit_streak_category(session, base_url, season, cat_cfg, pool_size):
    pool = build_candidate_pool(
        session, base_url, season, cat_cfg["seed_leaderboards"], "hitting", pool_size
    )
    records = []
    for person_id, player in pool.items():
        game_log = get_game_log(session, base_url, person_id, season, "hitting")
        if not game_log:
            continue
        streak, last_game_date = compute_hit_streak(game_log)
        if streak <= 0:
            continue
        records.append(
            {
                "entity": player["name"],
                "team": player["team"],
                "stat_category": cat_cfg["key"],
                "window": "active_streak",
                "value": streak,
                "last_game_date": last_game_date,
            }
        )
    return records


def fetch(config):
    """Fetch raw (unranked) records for every configured MLB stat category."""
    mlb_cfg = config["mlb"]
    base_url = mlb_cfg["base_url"]
    season = mlb_cfg["season"]
    window_games = mlb_cfg["window_games"]
    pool_size = mlb_cfg["candidate_pool_size"]

    session = requests.Session()
    records = []
    for cat_cfg in mlb_cfg["stat_categories"]:
        if cat_cfg["mode"] == "rolling_sum":
            records.extend(
                fetch_rolling_sum_category(session, base_url, season, window_games, cat_cfg, pool_size)
            )
        elif cat_cfg["mode"] == "hit_streak":
            records.extend(fetch_hit_streak_category(session, base_url, season, cat_cfg, pool_size))
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
