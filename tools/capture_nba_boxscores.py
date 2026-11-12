"""One-off capture of real ESPN NBA box scores, run from GitHub's runners
because the build sandbox cannot reach site.api.espn.com. Writes the fixture
tools/verify/nba_boxscores_fixture.json: the raw scoreboard events of the
window (trimmed to the fields fetchers.nba reads) and each decided counted
game's boxscore.players block."""
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
                "names": g.get("names"), "keys": g.get("keys"), "labels": g.get("labels"),
                "athletes": [{
                    "athlete": {"id": (a.get("athlete") or {}).get("id"),
                                "displayName": (a.get("athlete") or {}).get("displayName"),
                                "position": {"abbreviation": ((a.get("athlete") or {}).get("position") or {}).get("abbreviation")}},
                    "starter": a.get("starter"), "didNotPlay": a.get("didNotPlay"),
                    "reason": a.get("reason"), "ejected": a.get("ejected"),
                    "stats": a.get("stats")} for a in g.get("athletes") or []]})
        out.append({"team": {k: team.get(k) for k in ("id", "abbreviation", "displayName")},
                    "statistics": groups})
    return out


summaries, sample, shapes = {}, None, {}
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
    if sample is None:
        sample = {"top_level_keys": sorted(data), "boxscore_keys": sorted(data.get("boxscore") or {}),
                  "players_full": players}
    summaries[row["id"]] = {"boxscore": {"players": trim_players(players)}}
print("summaries:", len(summaries))
json.dump({"captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "window": [start.isoformat(), through.isoformat()],
           "events": [trim_event(e) for e in keep], "summaries": summaries},
          open("tools/verify/nba_boxscores_fixture.json", "w"), separators=(",", ":"))
json.dump(sample, open("tools/verify/nba_boxscore_sample_full.json", "w"), indent=1)
