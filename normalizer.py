"""Converts each sport's raw fetcher output into one common schema:

    {sport, competition, entity, entity_id, team, position, stat_category,
     window, value, rank, last_game_date, series}

`rank` is left unset here -- ranking is a separate pipeline stage
(see generate_stats.py) since it depends on how records for a given
stat_category + window are grouped and sorted.

Every fetcher is expected to emit raw records shaped like:
    {"entity": str, "entity_id": str | int | None, "team": str | None,
     "team_id": int | None, "position": str | None, "stat_category": str, "window": str,
     "value": int | float, "last_game_date": str | None,
     "series": list | None}

`entity_id` and `series` are optional passthroughs: `entity_id` lets a
post-ranking enrichment step re-query a specific player's per-game log
without a name lookup; `series` carries a fetcher-supplied per-game/match
value list when the fetcher already has it for free (World Cup does; MLB
attaches it in a later enrichment pass, once ranking has trimmed the field
down to the players actually worth an extra call).
"""


def normalize(sport, competition, raw_records):
    normalized = []
    for r in raw_records:
        normalized.append(
            {
                "sport": sport,
                "competition": competition,
                "entity": r["entity"],
                "entity_id": r.get("entity_id"),
                "team": r.get("team"),
                "team_id": r.get("team_id"),
                "position": r.get("position"),
                "stat_category": r["stat_category"],
                "window": r["window"],
                "value": r["value"],
                "rank": None,
                "last_game_date": r.get("last_game_date"),
                "series": r.get("series"),
            }
        )
    return normalized
