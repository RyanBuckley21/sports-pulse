"""Fetcher for Premier League "Who's Hot" leaderboards via ESPN's public
(undocumented but stable) site API.

LEADERBOARD ONLY. This module registers into generate_stats.SPORT_FETCHERS and
nothing else. There is no EPL entry in generate_insights.GAME_BUILDERS, no
`betting_signals.epl` config block, no weights, no thresholds-as-conviction,
nothing graded, and no ledger rows. These are descriptive boards: players
ranked by raw production over a trailing window, full stop.

Relationship to fetchers/worldcup.py (now archived -- see docs/leagues.md):
EPL is served by the same ESPN endpoints at a different competition path
(`soccer/eng.1` rather than `soccer/fifa.world`), and the per-match payload is
genuinely identical in shape -- verified live against real 2025-26 fixtures.
Three functions are therefore lifted from it essentially unchanged, because
they parse that shared payload rather than encoding anything tournament-
specific:

    classify_position          granular ESPN position -> broad bucket
    extract_player_stat_rows   per-match per-player stat lines
    extract_clean_sheet_credits  starting keeper, no goals conceded, never
                               subbed, with the two-signal safeguard intact

What is deliberately NOT lifted, because it is World-Cup-shaped and wrong for
a league:

  * KNOCKOUT_STAGE_SLUGS / get_eliminated_teams -- a league has no bracket and
    nobody is "eliminated"; every club plays all 38 matches.
  * get_tournament_start_date -- reads leagues[0].season.startDate, which for
    a league returns the CURRENT season window rather than the season of the
    events being queried (verified: querying August 2025 fixtures reports a
    2026-06-01 season start). A rolling window needs no season anchor at all.
  * tournament_to_date windowing -- a 38-match league needs a rolling window
    over recent matches, not a cumulative season total.

Roughly 40% of worldcup.py is reusable here; the discarded part is its whole
eligibility and windowing model.

Attribution/licence: ESPN publishes no terms for these endpoints. Same
unofficial-API risk class the repo already carries for statsapi.mlb.com and
this file's World Cup predecessor.
"""

import collections
import datetime

import requests

REQUEST_TIMEOUT = 15

# ESPN's scoreboard silently returns only the first 100 events unless `limit`
# is passed -- no error, no flag, no cursor. A full 380-match EPL season
# returns 100 without it and 380 with it (measured). Every call here that
# iterates `events` passes it. Same constant, same reason, as
# fetchers/worldcup.SCOREBOARD_LIMIT.
SCOREBOARD_LIMIT = 1000

# ESPN reports a player's position FOR THAT MATCH, and every unused/benched
# player comes back as "Substitute" rather than their real role. Measured over
# 70 real 2025-26 matches: 27% of all appearances carry "Substitute", and 16%
# of players (75 of 457) have no other position recorded at all. So position
# is a per-match role here, not a player attribute -- see stable_positions.
_BENCH_POSITION = "Substitute"

# How far back find_last_completed_date may hunt for the most recent finished
# match when the live window is empty. A full year, because the thing being
# searched for is on the far side of an offseason and its distance varies with
# where in that offseason the run happens.
#
# Costs ONE scoreboard call and no summary calls, so the span is cheap to
# overshoot -- and `limit=1000` (see SCOREBOARD_LIMIT) covers a 380-match
# season plus the surrounding fixtures without truncating.
OFFSEASON_SEARCH_DAYS = 365


def classify_position(position_name):
    """Granular ESPN position name -> one of the four broad buckets the UI
    groups by. Lifted from fetchers/worldcup.py unchanged; verified against
    real EPL data, where it correctly maps every position seen ("Center Left
    Defender" -> Defender, "Attacking Midfielder Left" -> Midfielder, ...).

    Note it passes "Substitute" through untouched, because "Substitute" is
    not a position -- callers must resolve a real position first (see
    stable_positions) rather than filtering on this function's output."""
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


def get_completed_events(session, scoreboard_url, start_compact, end_compact):
    """Matches ESPN marks as finished in a date range, newest last.

    Checks the status type's `completed` flag rather than matching an exact
    status name -- worldcup.py's own hard-won lesson, since a match decided
    beyond 90 minutes reports STATUS_FINAL_AET / STATUS_FINAL_PENALTIES
    instead of STATUS_FULL_TIME. League matches rarely go to extra time, but
    the flag is correct regardless and costs nothing."""
    data = _get(session, scoreboard_url,
                params={"dates": "{}-{}".format(start_compact, end_compact),
                        "limit": SCOREBOARD_LIMIT})
    events = []
    for event in data.get("events", []):
        if not event.get("status", {}).get("type", {}).get("completed"):
            continue
        events.append({"id": event["id"], "date": event["date"][:10]})
    events.sort(key=lambda e: e["date"])
    return events


def find_last_completed_date(session, scoreboard_url, today,
                             search_days=OFFSEASON_SEARCH_DAYS):
    """Date of the most recent completed match on or before `today`, or None if
    the league has played nothing in `search_days`.

    Exists to anchor the offseason fallback in fetch(): between seasons there is
    no "last 75 days of football" to window over, so the window has to be hung
    off the last day football actually happened instead of off today.

    Deliberately one scoreboard call and ZERO summary calls -- it answers "when
    did the league last play" and nothing else. The expensive per-match walk
    still happens once, afterwards, over the narrow window this anchors."""
    start = today - datetime.timedelta(days=search_days)
    events = get_completed_events(session, scoreboard_url,
                                  start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
    if not events:
        return None
    # get_completed_events sorts oldest-first, so the newest is last.
    return datetime.date.fromisoformat(events[-1]["date"])


def get_match_summary(session, summary_url, event_id):
    return _get(session, summary_url, params={"event": event_id})


def extract_player_stat_rows(summary_data):
    """Per-player stat lines for everyone who appeared in one match. Lifted
    from fetchers/worldcup.py; the EPL payload carries the same stat names
    (appearances, totalGoals, goalAssists, totalShots, shotsOnTarget,
    goalsConceded) plus a few World Cup never used (saves, foulsCommitted,
    cards).

    The raw per-match position is kept as `position_raw` rather than being
    classified here, because a single match cannot tell you a player's real
    position -- a bench appearance reports "Substitute". Resolution happens
    across the whole window in stable_positions()."""
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
                    "position_raw": (player.get("position") or {}).get("name"),
                    "stats": stat_map,
                }
            )
    return rows


def extract_clean_sheet_credits(summary_data):
    """Starting goalkeepers whose team conceded 0 in a match they played start
    to finish. Lifted from fetchers/worldcup.py with its safeguard intact, and
    that safeguard matters more here, not less.

    `goalsConceded` alone is not trustworthy: if a backup keeper comes on we
    cannot tell whether the field is scoped to the whole match or just that
    keeper's time on the pitch. An adjacent field, `shotsFaced`, is outright
    wrong -- re-confirmed on real EPL data, where a keeper who conceded 2 and
    made 1 save reported shotsFaced 0. So a clean sheet counts only when BOTH
    hold: the starter's own roster entry shows they were never subbed out, AND
    no substitution event in the match involves any goalkeeper on their team.
    """
    keeper_ids = set()
    starters = []
    for team_roster in summary_data.get("rosters", []):
        team_name = team_roster.get("team", {}).get("displayName")
        for player in team_roster.get("roster", []):
            if (player.get("position") or {}).get("name") != "Goalkeeper":
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
        s for s in starters
        if s["team"] not in keeper_sub_teams and not s["subbed_out"] and s["goals_conceded"] == 0
    ]


# --------------------------------------------------------------------------- #
# Window assembly -- rolling, per player, transfer-aware.
# --------------------------------------------------------------------------- #

def collect_appearances(session, scoreboard_url, summary_url, start_compact, end_compact):
    """{player_id: [appearance, ...]} oldest-first across every completed match
    in the range, where each appearance is one player's line from one match.

    Cost is one scoreboard call plus one summary call per match. Measured on
    real data: ~8s for a 50-match window, which is what a 5-matchday rolling
    window costs. A whole 380-match season would be ~60s, which is why the
    caller windows by date rather than pulling the season and slicing."""
    events = get_completed_events(session, scoreboard_url, start_compact, end_compact)
    by_player = collections.defaultdict(list)

    for event in events:
        summary_data = get_match_summary(session, summary_url, event["id"])
        rows = {row["id"]: row for row in extract_player_stat_rows(summary_data)}
        # Fold the clean-sheet credit into the keeper's own row for the match,
        # so it flows through the generic per-category pipeline like any other
        # stat and does not create a second appearance for the same match.
        for credit in extract_clean_sheet_credits(summary_data):
            row = rows.setdefault(credit["id"], {
                "id": credit["id"], "name": credit["name"], "team": credit["team"],
                "position_raw": "Goalkeeper", "stats": {},
            })
            row["stats"]["cleanSheets"] = 1

        for row in rows.values():
            by_player[row["id"]].append({
                "date": event["date"],
                "name": row["name"],
                "team": row["team"],
                "position_raw": row.get("position_raw"),
                "stats": row["stats"],
            })

    for appearances in by_player.values():
        appearances.sort(key=lambda a: a["date"])
    return by_player


def stable_position(appearances):
    """One position for a player, from their MODAL NON-BENCH position across
    the window, or None when they only ever appeared off the bench.

    ESPN reports position per match, and a benched player is reported as
    "Substitute" -- not their real role. Measured across 70 real 2025-26
    matches, 27% of appearances carry "Substitute" and 16% of players have no
    other position on record. Filtering a board on the raw per-match value
    would therefore invent a "Substitute" bucket and misclassify a quarter of
    appearances (Federico Chiesa scored a goal while listed as one).

    Returning None for a bench-only player is deliberate and load-bearing: we
    genuinely do not know their position, so they are excluded from
    position-filtered boards rather than guessed into one. They remain fully
    eligible for the unfiltered boards (Goals, Assists, Goal Contributions),
    where position is irrelevant -- a substitute's goal counts exactly like a
    starter's."""
    counts = collections.Counter(
        a["position_raw"] for a in appearances
        if a.get("position_raw") and a["position_raw"] != _BENCH_POSITION
    )
    if not counts:
        return None
    # max() over (count, name) keeps ties deterministic rather than
    # dict-insertion-ordered.
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return classify_position(top)


def aggregate_category(by_player, cat_cfg, default_window):
    """Rank-ready raw records for ONE category.

    The window is a player's most recent `window_games` APPEARANCES, not
    calendar matchdays -- so rotation, injury and suspension shift the window
    back rather than punching holes in it. `min_games` then drops anyone whose
    window is too thin to read as form.

    TRANSFERS ARE NOT SPLIT. A player who changes club mid-window keeps one
    continuous window: their last five appearances are their last five
    appearances, and that IS their recent form. Splitting would erase a
    just-transferred player from every board exactly when they are most
    interesting. Team attribution instead follows their MOST RECENT
    appearance, so the board shows their current club -- the same rule
    fetchers/nfl.py applies to a mid-season trade. Measured on real 2025-26
    data, 11 players turned out for more than one club in a season, so this is
    a routine case rather than an edge one.

    `per_appearance` divides by the number of appearances in the player's own
    window. It is explicitly NOT a per-90: ESPN's payload carries no minutes
    played, only an appearances flag, so a 10-minute cameo and a full 90 count
    the same. Every label for such a category must say "per appearance" and
    never "per 90" -- see config.yaml's epl block.
    """
    fields = cat_cfg["fields"]
    window_games = cat_cfg.get("window_games", default_window)
    min_games = cat_cfg.get("min_games", 1)
    per_appearance = bool(cat_cfg.get("per_appearance"))
    positions = cat_cfg.get("positions")

    records = []
    for player_id, all_appearances in by_player.items():
        window = all_appearances[-window_games:]
        if len(window) < min_games:
            continue

        if positions:
            # Resolved over the player's WHOLE window, not per appearance, so
            # a striker who came off the bench once is still a Forward.
            pos = stable_position(all_appearances)
            if pos is None or pos not in positions:
                continue
        else:
            pos = stable_position(all_appearances)

        per_match = [sum(a["stats"].get(f, 0) or 0 for f in fields) for a in window]
        total = sum(per_match)
        if total <= 0:
            continue
        value = round(total / len(window), 2) if per_appearance else int(total)

        latest = window[-1]
        records.append({
            "entity": latest["name"],
            "entity_id": player_id,
            "team": latest["team"],          # current club: most recent appearance
            "team_id": None,
            "position": pos,
            "stat_category": cat_cfg["key"],
            "window": "last_{}_appearances".format(window_games) + ("_per_appearance" if per_appearance else ""),
            "value": value,
            "tiebreak": None,
            "games_window": len(window),
            "last_game_date": latest["date"],
            "series": [{"date": a["date"], "value": int(v)} for a, v in zip(window, per_match)],
        })
    return records


def _build_records(by_player, epl_cfg, default_window):
    """Rank-ready records for every configured stat category, or [] when the
    appearances given fill no board at all.

    Split out of fetch() because "did this window produce a board?" is the
    condition the offseason fallback turns on, and that is NOT the same
    question as "did this window contain any matches". A window can hold real
    football and still yield nothing: on 2026-08-07 the 75-day window reached
    back to exactly 2026-05-24, catching one matchday, so every player had a
    single appearance and `min_games: 3` filtered all of them out. Keying the
    fallback on the matches found would leave that day's board empty; keying it
    on the records built is what makes the guard mean what it says.

    ONE DELIBERATE SIDE EFFECT: the unknown-mode ValueError below now fires even
    when the window held no matches. fetch() used to return [] before reaching
    this loop on such a day, so a typo'd `mode` in config was silently masked by
    the calendar and surfaced only once football resumed. Validating regardless
    of what the window happened to contain is the more useful behaviour -- a
    misconfiguration should not be discoverable only in season."""
    records = []
    for cat_cfg in epl_cfg["stat_categories"]:
        if cat_cfg["mode"] != "rolling_sum":
            raise ValueError("Unknown EPL stat category mode: {}".format(cat_cfg["mode"]))
        records.extend(aggregate_category(by_player, cat_cfg, default_window))
    return records


def fetch(config, today=None):
    """Raw (unranked) Who's Hot records for every configured EPL stat category
    -- generate_stats.SPORT_FETCHERS' entry point for this sport.

    Windows by DATE (a lookback wide enough to contain `window_games`
    appearances for a regular starter) rather than pulling the whole season,
    because each match costs a summary call. `lookback_days` is generous on
    purpose: a player who has been rotated or injured needs a wider calendar
    span to accumulate five appearances, and matches outside anyone's window
    are simply never selected by aggregate_category.

    OFFSEASON FALLBACK. A rolling window anchored to TODAY holds nothing at all
    between seasons, and the gap is much wider than the window: measured live,
    2025-26 ended 2026-05-24 and 2026-27 begins 2026-08-21, an 89-day gap
    against a 75-day lookback. So on 2026-08-20 the primary window began
    2026-06-06 -- thirteen days AFTER the last match ever played -- and the
    boards rendered empty rather than stale.

    When the primary window FILLS NO BOARD, the window is therefore re-anchored
    to the last day the league actually played and the same `lookback_days` span
    is taken backwards from there. That yields the closing matchdays of the
    previous season, which is the honest answer to "who is hot" when nobody has
    kicked a ball since May -- last season's form is the only form there is.

    "Fills no board" and "found no matches" are not the same test, and the
    difference is load-bearing for three days a year. On 2026-08-07 the 75-day
    window reached back to exactly 2026-05-24 and caught a single matchday: real
    football, so a matches-found test would pass and the fallback would stay
    down, but every player held one appearance against `min_games: 3` and the
    board came out empty anyway. The condition is therefore the records built,
    not the matches seen -- see _build_records.

    Re-anchoring rather than simply WIDENING the window is what keeps this
    cheap and self-correcting. Widening enough to clear an offseason (~121 days
    on 2026-08-20) would fetch every match in that span -- ~130 summary calls
    for data aggregate_category then throws away, since it only ever keeps each
    player's last `window_games` appearances -- and the number needed would
    depend on the calendar date of the run. Anchoring keeps the fetch at its
    usual ~75 matches whatever the date, and needs no tuning per offseason.

    The fallback is self-limiting: it fires only when the live window fills no
    board, so it costs nothing in-season, and it stops firing on its own once
    the new season has enough matches to fill one.

    IT DOES NOT COVER THE CROSSOVER. Between the new season's first match and
    the point where players reach `min_games`, the anchor lands INSIDE the live
    window -- there is no earlier football to fall back to that this window does
    not already hold -- so the board stays empty rather than reverting to last
    season. Carrying the previous season across that crossover means boards
    mixing two seasons, which is a UI/labelling decision and not one to make
    silently inside a fetcher. See the equal-windows branch below.
    """
    epl_cfg = config["epl"]
    scoreboard_url = epl_cfg["scoreboard_url"]
    summary_url = epl_cfg["summary_url"]
    default_window = epl_cfg.get("window_games", 5)
    lookback_days = epl_cfg.get("lookback_days", 75)

    today = today or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    start = today - datetime.timedelta(days=lookback_days)

    session = requests.Session()
    by_player = collect_appearances(
        session, scoreboard_url, summary_url,
        start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
    records = _build_records(by_player, epl_cfg, default_window)

    if not records:
        # Offseason, or a window catching too little football to fill a board.
        # See the OFFSEASON FALLBACK note above.
        anchor = find_last_completed_date(session, scoreboard_url, today)
        anchored_start = (anchor - datetime.timedelta(days=lookback_days)) if anchor else None

        if anchor is None:
            print("whos-hot(epl): no completed matches in the last {} days, and none in "
                  "the {} days searched behind that -- no boards"
                  .format(lookback_days, OFFSEASON_SEARCH_DAYS))
            return []
        if anchored_start == start:
            # The anchored window IS the window just tried, so re-fetching it
            # would spend the same summary calls to reach the same empty board.
            # This is the new season's opening matchdays: football has resumed
            # (so there is nothing to fall back FROM -- the anchor is inside the
            # live window) but no player has yet reached `min_games`. Carrying
            # the previous season across that crossover is a different feature
            # with a UI question attached -- what a board mixing two seasons is
            # called -- and is deliberately not decided here.
            print("whos-hot(epl): matches found in the last {} days but no player has "
                  "enough appearances to fill a board yet -- no boards"
                  .format(lookback_days))
            return []

        print("whos-hot(epl): the last {} days fill no board; the league last played {} "
              "({} days ago), so windowing the {} days ending then"
              .format(lookback_days, anchor, (today - anchor).days, lookback_days))
        by_player = collect_appearances(
            session, scoreboard_url, summary_url,
            anchored_start.strftime("%Y%m%d"), anchor.strftime("%Y%m%d"))
        records = _build_records(by_player, epl_cfg, default_window)
        if not records:
            print("whos-hot(epl): the {} days ending {} filled no board either -- no boards"
                  .format(lookback_days, anchor))
            return []

    return records
