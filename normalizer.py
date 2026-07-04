"""Converts each sport's raw fetcher output into one common schema:

    {sport, competition, entity, team, stat_category, window, value,
     rank, last_game_date}

`rank` is left unset here -- ranking is a separate pipeline stage
(see generate_stats.py) since it depends on how records for a given
stat_category + window are grouped and sorted.

Every fetcher is expected to emit raw records shaped like:
    {"entity": str, "team": str | None, "stat_category": str,
     "window": str, "value": int | float, "last_game_date": str | None}
"""


def normalize(sport, competition, raw_records):
    normalized = []
    for r in raw_records:
        normalized.append(
            {
                "sport": sport,
                "competition": competition,
                "entity": r["entity"],
                "team": r.get("team"),
                "stat_category": r["stat_category"],
                "window": r["window"],
                "value": r["value"],
                "rank": None,
                "last_game_date": r.get("last_game_date"),
            }
        )
    return normalized
