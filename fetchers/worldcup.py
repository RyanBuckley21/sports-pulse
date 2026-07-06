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

# Single-elimination rounds, in order, after the group stage. Any team that
# doesn't turn up in one of these once the bracket is set has failed to
# advance; any team that loses one of these matches is out.
KNOCKOUT_STAGE_SLUGS = {"round-of-32", "round-of-16", "quarterfinals", "semifinals", "third-place", "final"}

# ESPN reports granular positions ("Center Left Defender", "Attacking
# Midfielder Left", ...); collapse to the four broad buckets the UI groups
# by. Order matters only in that each keyword is unambiguous on its own.
def classify_position(position_name):
    if not position_name:
        return None
    name = position_name.lower()
    if "goalkeeper" in name:
        return "Goalkeeper"
    if "back" in name or "defender" in name:
        return "Defender"
    if "midfielder" in name:
        return "Midfielder"
    if "forward" in name:
        return "Forward"
    return position_name


def _get(session, url, params=None):
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_tournament_start_date(session, scoreboard_url):
    """Tournament start date (from ESPN's season metadata), as an ISO date
    string (YYYY-MM-DD)."""
    data = _get(session, scoreboard_url)
    leagues = data.get("leagues", [])
    if not leagues:
        raise ValueError("ESPN scoreboard response had no league/season metadata")
    return leagues[0]["season"]["startDate"][:10]


def get_eliminated_teams(session, scoreboard_url, start_compact, end_compact):
    """Team display names that are out of the tournament: they either never
    reached the knockout bracket once it was set, or lost a knockout match.
    `end_compact` should reach well past today, since a team's advancement
    into the next round is visible as soon as it's slotted in -- not just
    from completed matches -- and the query is otherwise cheap (one call)."""
    data = _get(session, scoreboard_url, params={"dates": f"{start_compact}-{end_compact}"})
    group_teams = set()
    knockout_teams = set()
    eliminated = set()
    knockout_stage_seen = False

    for event in data.get("events", []):
        slug = event.get("season", {}).get("slug")
        competitors = event.get("competitions", [{}])[0].get("competitors", [])
        names = [c.get("team", {}).get("displayName") for c in competitors]
        if slug == "group-stage":
            group_teams.update(n for n in names if n)
        elif slug in KNOCKOUT_STAGE_SLUGS:
            knockout_stage_seen = True
            knockout_teams.update(n for n in names if n)
            if event.get("status", {}).get("type", {}).get("completed"):
                for c in competitors:
                    if c.get("winner") is False:
                        name = c.get("team", {}).get("displayName")
                        if name:
                            eliminated.add(name)

    if knockout_stage_seen:
        eliminated.update(group_teams - knockout_teams)
    return eliminated


def get_completed_events(session, scoreboard_url, start, end):
    """Matches ESPN marks as finished. Knockout-stage games that go to extra
    time or penalties get a different status name (STATUS_FINAL_AET,
    STATUS_FINAL_PENALTIES, ...) than a regular STATUS_FULL_TIME game, so we
    check the type's `completed` flag rather than matching one exact name --
    otherwise any match decided beyond 90 minutes silently drops out."""
    data = _get(session, scoreboard_url, params={"dates": f"{start}-{end}"})
    events = []
    for event in data.get("events", []):
        if not event.get("status", {}).get("type", {}).get("completed"):
            continue
        events.append({"id": event["id"], "date": event["date"][:10]})
    return events


def get_match_summary(session, summary_url, event_id):
    return _get(session, summary_url, params={"event": event_id})


def extract_player_stat_rows(summary_data):
    """Per-player stat lines for everyone who appeared in one match."""
    rows = []
    for team_roster in summary_data.get("rosters", []):
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
                    "position": classify_position(player.get("position", {}).get("name")),
                    "stats": stat_map,
                }
            )
    return rows


def extract_clean_sheet_credits(summary_data):
    """Starting goalkeepers whose team conceded 0 in a match they played
    start to finish. `goalsConceded` on its own isn't trustworthy for this --
    if a backup keeper subs on, we don't know whether that field is scoped to
    the whole match or just the sub's time on the pitch (and some adjacent
    ESPN fields, like shotsFaced, have been outright wrong in spot checks) --
    so a clean sheet only counts when BOTH hold: the starter's own roster
    entry shows they were never subbed out, AND no "Substitution" event in
    the match involves any goalkeeper on their team. Either signal alone
    could be missed or stale; requiring both is the safeguard."""
    keeper_ids = set()
    starters = []
    for team_roster in summary_data.get("rosters", []):
        team_name = team_roster.get("team", {}).get("displayName")
        for player in team_roster.get("roster", []):
            if player.get("position", {}).get("name") != "Goalkeeper":
                continue
            keeper_ids.add(player["athlete"]["id"])
            if player.get("starter"):
                stat_map = {s["name"]: s.get("value", 0) for s in player.get("stats", [])}
                starters.append(
                    {
                        "team": team_name,
                        "id": player["athlete"]["id"],
                        "name": player["athlete"]["fullName"],
                        "subbed_out": bool(player.get("subbedOut")),
                        "goals_conceded": stat_map.get("goalsConceded"),
                    }
                )

    keeper_sub_teams = set()
    for event in summary_data.get("keyEvents", []):
        if event.get("type", {}).get("type") != "substitution":
            continue
        participant_ids = {p.get("athlete", {}).get("id") for p in event.get("participants", [])}
        if participant_ids & keeper_ids:
            team_name = event.get("team", {}).get("displayName")
            if team_name:
                keeper_sub_teams.add(team_name)

    return [
        s
        for s in starters
        if s["team"] not in keeper_sub_teams and not s["subbed_out"] and s["goals_conceded"] == 0
    ]


def aggregate_tournament_stats(session, scoreboard_url, summary_url, start_compact, today_compact):
    """Sum every player's per-match stat lines across all completed matches,
    plus a cleanSheets count folded into the same per-player stats dict so
    it flows through the generic per-category pipeline like any other stat.
    Also retains each match's individual (already-merged) stat line in
    `series`, so the detail view can show a real per-match breakdown instead
    of just the tournament-to-date total.

    A clean-sheet credit and a player's regular stat row for the same match
    are merged into one series entry per match (not two) -- both ultimately
    describe the same player-match, and keeping them separate would double
    the series length for any credited goalkeeper without changing the
    running totals, which are unaffected either way since dict-keyed
    summation doesn't care whether it's called once or twice per match."""
    events = get_completed_events(session, scoreboard_url, start_compact, today_compact)

    players = {}

    def add_stats(person_id, name, team, position, event_date, stat_updates):
        entry = players.setdefault(
            person_id,
            {"entity": name, "team": team, "position": position, "stats": {}, "last_game_date": None, "series": []},
        )
        for stat_name, value in stat_updates.items():
            entry["stats"][stat_name] = entry["stats"].get(stat_name, 0) + (value or 0)
        entry["series"].append({"date": event_date, "stats": stat_updates})
        if entry["last_game_date"] is None or event_date > entry["last_game_date"]:
            entry["last_game_date"] = event_date

    for event in events:
        summary_data = get_match_summary(session, summary_url, event["id"])

        match_rows = {row["id"]: row for row in extract_player_stat_rows(summary_data)}
        for credit in extract_clean_sheet_credits(summary_data):
            row = match_rows.setdefault(
                credit["id"],
                {"id": credit["id"], "name": credit["name"], "team": credit["team"], "position": "Goalkeeper", "stats": {}},
            )
            row["stats"]["cleanSheets"] = 1

        for row in match_rows.values():
            add_stats(row["id"], row["name"], row["team"], row.get("position"), event["date"], row["stats"])

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
    World Cup stat category, restricted to players on teams still alive."""
    wc_cfg = config["worldcup"]
    scoreboard_url = wc_cfg["scoreboard_url"]
    summary_url = wc_cfg["summary_url"]
    session = requests.Session()

    start_iso = get_tournament_start_date(session, scoreboard_url)
    start_compact = start_iso.replace("-", "")
    today_compact = datetime.date.today().strftime("%Y%m%d")
    # Wide enough to see the whole revealed bracket (not just played matches),
    # since a team's advancement is known as soon as it's slotted into the
    # next round -- comfortably past a ~5-week World Cup schedule.
    wide_end_compact = (datetime.date.fromisoformat(start_iso) + datetime.timedelta(days=60)).strftime("%Y%m%d")

    eliminated_teams = get_eliminated_teams(session, scoreboard_url, start_compact, wide_end_compact)
    players = aggregate_tournament_stats(session, scoreboard_url, summary_url, start_compact, today_compact)
    players = {pid: p for pid, p in players.items() if p["team"] not in eliminated_teams}

    records = []
    for cat_cfg in wc_cfg["stat_categories"]:
        window = "tournament_to_date" + ("_per_game" if cat_cfg.get("per_game") else "")
        for person_id, player in players.items():
            value = compute_category_value(player["stats"], cat_cfg)
            if not value:
                continue
            # Per-match value for this category = the same configured fields,
            # summed within just that one match's stat line -- consistent
            # with how the tournament-to-date total is computed, just scoped
            # to a single series entry instead of the whole tournament.
            series = [
                {"date": s["date"], "value": int(sum(s["stats"].get(f, 0) for f in cat_cfg["fields"]))}
                for s in player["series"]
            ]
            records.append(
                {
                    "entity": player["entity"],
                    "entity_id": person_id,
                    "team": player["team"],
                    "position": player.get("position"),
                    "stat_category": cat_cfg["key"],
                    "window": window,
                    "value": value if cat_cfg.get("per_game") else int(value),
                    "last_game_date": player["last_game_date"],
                    "series": series,
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
