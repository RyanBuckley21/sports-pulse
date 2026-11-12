"""Capture of real ESPN NBA box scores for tools/verify/nba_boxscores_fixture.json
(test_nba_leaderboards reads it). Manual, and not part of the pipeline.

    python3 -m tools.capture_nba_boxscores [DAYS=14] [THROUGH=today, Eastern]

It has to run somewhere that can reach site.api.espn.com, which the build
sandbox cannot: the committed fixture was captured on 2026-11-12T00:13Z from a
GitHub runner (run 43125639054, a temporary step on the feature branch), 117
decided games from 2026-10-26 to 2026-11-10, 8 seconds for the season's
scoreboard months plus 117 summary requests.

The fixture is the feed trimmed to the fields fetchers.nba reads and nothing
hand-written: each window event's id, date, season, competition type, status
and competitors, and each decided counted game's boxscore.players with the
stat keys and every athlete's id, name, position, starter, didNotPlay and
stat strings."""
import datetime, json, sys
import requests
import slate_clock
from fetchers import nba

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
THROUGH = sys.argv[2] if len(sys.argv) > 2 else slate_clock.eastern_date()

through = datetime.date.fromisoformat(THROUGH)
start = through - datetime.timedelta(days=DAYS)
session = requests.Session()
events = nba.fetch_events_through(session, nba.season_year_for_date(through), through)
keep = []
for e in events:
    day = nba.slate_date(e)
    if day and start.isoformat() <= day <= through.isoformat():
        keep.append(e)
print("events in window:", len(keep))


def trim_event(e):
    comp = (e.get("competitions") or [{}])[0]
    return {"id": e.get("id"), "date": e.get("date"), "season": e.get("season"),
            "competitions": [{
                "type": comp.get("type"), "neutralSite": comp.get("neutralSite"),
                "status": {"type": {k: ((comp.get("status") or {}).get("type") or {}).get(k)
                                    for k in ("name", "state", "completed")}},
                "competitors": [{"homeAway": c.get("homeAway"), "score": c.get("score"),
                                 "team": {k: (c.get("team") or {}).get(k)
                                          for k in ("id", "abbreviation", "displayName")}}
                                for c in comp.get("competitors") or []]}]}


def trim_players(players):
    out = []
    for t in players or []:
        team = t.get("team") or {}
        groups = []
        for g in t.get("statistics") or []:
            groups.append({
                "keys": g.get("keys"),
                "athletes": [{
                    "athlete": {"id": (a.get("athlete") or {}).get("id"),
                                "displayName": (a.get("athlete") or {}).get("displayName"),
                                "position": {"abbreviation": ((a.get("athlete") or {}).get("position") or {}).get("abbreviation")}},
                    "starter": a.get("starter"), "didNotPlay": a.get("didNotPlay"),
                    "stats": a.get("stats")} for a in g.get("athletes") or []]})
        out.append({"team": {k: team.get(k) for k in ("id", "abbreviation", "displayName")},
                    "statistics": groups})
    return out


summaries = {}
for e in keep:
    if not nba.is_counted_game(e):
        continue
    row = nba.parse_game(e)
    if row is None or not row["completed"]:
        continue
    r = session.get(SUMMARY_URL, params={"event": row["id"]}, timeout=30)
    print(row["id"], row["slate_date"], row["away"], "@", row["home"], r.status_code)
    r.raise_for_status()
    data = r.json()
    players = (data.get("boxscore") or {}).get("players")
    summaries[row["id"]] = {"boxscore": {"players": trim_players(players)}}
print("summaries:", len(summaries))
json.dump({"captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "window": [start.isoformat(), through.isoformat()],
           "events": [trim_event(e) for e in keep], "summaries": summaries},
          open("tools/verify/nba_boxscores_fixture.json", "w"), separators=(",", ":"))
