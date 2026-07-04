"""Fetcher for FIFA World Cup 2026 data via ESPN's public (undocumented but
stable) site API.

Strategy for "who's hot" stats: the World Cup is a short, mostly
single-elimination tournament, so there's no meaningful rolling window --
"hot" here means tournament-to-date totals.
  1. Pull every completed match from the tournament's scoreboard (a
     date-ranged query from the tournament start date through today).
  2. Pull each match's summary, which includes a full per-player stat line
     (shots, goals, assists) for everyone who actually appeared.
  3. Sum each player's stats across every match they appeared in.
"""

import datetime

import requests

REQUEST_TIMEOUT = 15


def _get(session, url, params=None):
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_tournament_date_range(session, scoreboard_url):
    """Tournament start date (from ESPN's season metadata) through today,
    formatted as the YYYYMMDD strings ESPN's dates-range query expects."""
    data = _get(session, scoreboard_url)
    leagues = data.get("leagues", [])
    if not leagues:
        raise ValueError("ESPN scoreboard response had no league/season metadata")
    start_date = leagues[0]["season"]["startDate"][:10]
    today = datetime.date.today().isoformat()
    return start_date.replace("-", ""), today.replace("-", "")


def get_completed_events(session, scoreboard_url, start, end):
    data = _get(session, scoreboard_url, params={"dates": f"{start}-{end}"})
    events = []
    for event in data.get("events", []):
        if event.get("status", {}).get("type", {}).get("name") != "STATUS_FULL_TIME":
            continue
        events.append({"id": event["id"], "date": event["date"][:10]})
    return events


def get_match_player_stats(session, summary_url, event_id):
    """Per-player stat lines for everyone who appeared in one match."""
    data = _get(session, summary_url, params={"event": event_id})
    rows = []
    for team_roster in data.get("rosters", []):
        team_name = team_roster.get("team", {}).get("displayName")
        for player in team_roster.get("roster", []):
            stat_map = {s["name"]: s.get("value", 0) for s in player.get("stats", [])}
            if not stat_map.get("appearances"):
                continue
            rows.append(
                {
                    "id": player["athlete"]["id"],
                    "name": player["athlete"]["fullName"],
                    "team": team_name,
                    "stats": stat_map,
                }
            )
    return rows


def aggregate_tournament_stats(session, scoreboard_url, summary_url):
    """Sum every player's per-match stat lines across all completed matches."""
    start, end = get_tournament_date_range(session, scoreboard_url)
    events = get_completed_events(session, scoreboard_url, start, end)

    players = {}
    for event in events:
        for row in get_match_player_stats(session, summary_url, event["id"]):
            entry = players.setdefault(
                row["id"],
                {"entity": row["name"], "team": row["team"], "stats": {}, "last_game_date": None},
            )
            for stat_name, value in row["stats"].items():
                entry["stats"][stat_name] = entry["stats"].get(stat_name, 0) + (value or 0)
            if entry["last_game_date"] is None or event["date"] > entry["last_game_date"]:
                entry["last_game_date"] = event["date"]
    return players


def compute_category_value(player_stats, cat_cfg):
    """Sum the configured fields; for `per_game` categories, divide by the
    player's actual appearances (games played) across the tournament so far,
    the same "true average" approach used for MLB's per-game categories."""
    total = sum(player_stats.get(field, 0) for field in cat_cfg["fields"])
    if not cat_cfg.get("per_game"):
        return total
    appearances = player_stats.get("appearances") or 0
    if not appearances:
        return None
    return round(total / appearances, 2)


def fetch(config):
    """Fetch raw (unranked) tournament-to-date records for every configured
    World Cup stat category."""
    wc_cfg = config["worldcup"]
    session = requests.Session()
    players = aggregate_tournament_stats(session, wc_cfg["scoreboard_url"], wc_cfg["summary_url"])

    records = []
    for cat_cfg in wc_cfg["stat_categories"]:
        window = "tournament_to_date" + ("_per_game" if cat_cfg.get("per_game") else "")
        for player in players.values():
            value = compute_category_value(player["stats"], cat_cfg)
            if not value:
                continue
            records.append(
                {
                    "entity": player["entity"],
                    "team": player["team"],
                    "stat_category": cat_cfg["key"],
                    "window": window,
                    "value": value if cat_cfg.get("per_game") else int(value),
                    "last_game_date": player["last_game_date"],
                }
            )
    return records


if __name__ == "__main__":
    import yaml

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    results = fetch(cfg)
    print(f"Fetched {len(results)} raw World Cup records")
    for r in results[:5]:
        print(r)
