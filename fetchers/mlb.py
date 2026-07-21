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


def team_side_ops(session, base_url, team_id, season, is_home, as_of_date, cache, window_days=14):
    """A team's recomputed OPS over its last `window_days` of completed games on
    one side (home games for the home team, road games for the away team). Cached
    per (team_id, is_home) so a doubleheader team isn't fetched twice. None if the
    team has no games on that side in the window."""
    key = (team_id, is_home)
    if key in cache:
        return cache[key]
    data = _get(
        session,
        f"{base_url}/teams/{team_id}/stats",
        params={"stats": "gameLog", "group": "hitting", "season": season},
    )
    stats = data.get("stats", [])
    splits = stats[0].get("splits", []) if stats else []
    cutoff = (datetime.date.fromisoformat(as_of_date) - datetime.timedelta(days=window_days)).isoformat()
    windowed = [
        s for s in splits
        if s.get("date") and cutoff <= s["date"] < as_of_date and bool(s.get("isHome")) == is_home
    ]
    ops = _recompute_ops(windowed)
    cache[key] = ops
    return ops


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
    Returns a rounded ERA float, or None if the bullpen threw no innings."""
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
        return None
    return round(9.0 * er / (outs / 3.0), 2)


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
    """A pitcher's season ERA as the raw API string ("3.20"), cached per id.
    None if unavailable (best-effort; never fails the build)."""
    if pitcher_id in cache:
        return cache[pitcher_id]
    era = None
    try:
        data = _get(
            session,
            f"{base_url}/people/{pitcher_id}/stats",
            params={"stats": "season", "group": "pitching", "season": season},
        )
        stats = data.get("stats", [])
        if stats and stats[0].get("splits"):
            era = stats[0]["splits"][0].get("stat", {}).get("era")
    except requests.RequestException:
        era = None
    cache[pitcher_id] = era
    return era


def _fmt_ops(v):
    """.812 style -- fixed 3 decimals, leading zero dropped."""
    if v is None:
        return None
    s = "{:.3f}".format(v)
    return s[1:] if s.startswith("0.") else s


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
    score = max(30, min(100, score))
    label = ("Scorching" if score >= 85 else "Hot" if score >= 70 else "Warm" if score >= 55 else "Notable")
    return {"score": score, "label": label}


def _build_one_game(session, base_url, season, game_date, g, boxscore_cache, touched,
                    ops_cache, era_cache, config, injured_ids):
    import team_meta  # local import: keeps the standalone `python3 fetchers/mlb.py` helper working
    import betting_signals

    teams = g.get("teams", {})
    away_t = teams.get("away", {}).get("team", {})
    home_t = teams.get("home", {}).get("team", {})
    away_id, home_id = away_t.get("id"), home_t.get("id")

    def teamref(t):
        meta = team_meta.get_team_meta("mlb", t.get("name")) or {}
        return {"abbr": meta.get("abbr") or t.get("abbreviation"),
                "name": t.get("teamName"), "color": meta.get("color")}

    away_ref, home_ref = teamref(away_t), teamref(home_t)

    # OPS: home team's home form, away team's road form (14d, recomputed).
    home_ops = team_side_ops(session, base_url, home_id, season, True, game_date, ops_cache) if home_id else None
    away_ops = team_side_ops(session, base_url, away_id, season, False, game_date, ops_cache) if away_id else None

    # Bullpen ERA (7d): true bullpen, boxscore-cached.
    home_pen = team_bullpen_era(session, base_url, home_id, game_date, boxscore_cache, touched) if home_id else None
    away_pen = team_bullpen_era(session, base_url, away_id, game_date, boxscore_cache, touched) if away_id else None

    # Season series (counts kept from the away team's perspective).
    series = season_series(session, base_url, away_id, home_id, season, game_date) if (away_id and home_id) else None

    # Probable starters + season ERA (best-effort; omit an unannounced side).
    away_pp = teams.get("away", {}).get("probablePitcher")
    home_pp = teams.get("home", {}).get("probablePitcher")
    probables = {}
    away_era = home_era = None
    if away_pp and away_pp.get("id"):
        away_era = _fmt_era(pitcher_season_era(session, base_url, away_pp["id"], season, era_cache))
        probables["away"] = {"name": away_pp.get("fullName"), **({"era": away_era} if away_era else {})}
    if home_pp and home_pp.get("id"):
        home_era = _fmt_era(pitcher_season_era(session, base_url, home_pp["id"], season, era_cache))
        probables["home"] = {"name": home_pp.get("fullName"), **({"era": home_era} if home_era else {})}

    # ---- Team-relative framing: surface the single most-notable side per
    # one-sided family; keep inherently-paired families combined. Both sides are
    # always preserved in `context` for the AI (display=standout, AI=complete).
    signals = []
    framed_ops = None
    if home_ops is not None or away_ops is not None:
        if (away_ops or -1) > (home_ops or -1):
            framed_ops = away_ops
            signals.append({"label": f"{away_ref['abbr']} road OPS (14d)", "value": _fmt_ops(away_ops), "tone": "pos"})
        else:
            framed_ops = home_ops
            signals.append({"label": f"{home_ref['abbr']} home OPS (14d)", "value": _fmt_ops(home_ops), "tone": "pos"})

    framed_pen = None
    if home_pen is not None or away_pen is not None:
        if (home_pen or -1) > (away_pen or -1):
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
    betting = betting_signals.score_game(
        config, "mlb",
        betting_signals.build_inputs(away_ref, home_ref, away_ops, home_ops,
                                     away_pen, home_pen, away_era, home_era, series),
        availability={"away_probable_out": away_out, "home_probable_out": home_out},
    )
    # The single most-notable market (deterministic; None if nothing clears the
    # standout bar). Drives the AI's one-sentence betting_note downstream.
    standout = betting_signals.top_market(
        betting, ((config.get("betting_signals") or {}).get("mlb") or {}).get("standout_threshold", 50))

    return {
        "gamePk": g.get("gamePk"),
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


def build_game_entities(config, game_date, boxscore_cache):
    """Build one deterministic Game insight entity for every game on `game_date`'s
    MLB slate (full slate -- uncapped, since "today's games" is already bounded).

    Returns (entities, pruned_boxscore_cache):
      - entities: ordered {str(gamePk): entity} in slate order; each entity carries
        away/home TeamRefs (abbr/name/color), start (ET), venue, probables, framed
        signals, a deterministic pulse, status, and a both-sides `context` block.
      - pruned_boxscore_cache: the input cache plus any newly fetched Final-game
        reliever lines, pruned to just the gamePks referenced this run (games that
        fall out of every team's 7d window drop off, keeping the file tiny)."""
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
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            ent = _build_one_game(
                session, base_url, season, game_date, g, boxscore_cache, touched,
                ops_cache, era_cache, config, injured_ids,
            )
            entities[str(g.get("gamePk"))] = ent

    pruned_cache = {pk: boxscore_cache[pk] for pk in touched if pk in boxscore_cache}
    return entities, pruned_cache


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
