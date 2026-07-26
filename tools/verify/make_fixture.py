"""Writes a small web/data.json so the browser suite can run without network.

web/data.json is the real path the app fetches and is gitignored (it is a build
artefact of generate_stats.py), so writing it here does not dirty the tree. A
real `python3 generate_stats.py` run produces the same file with live data; this
just makes the suite runnable offline and deterministic.

    python3 -m tools.verify.make_fixture
"""

import datetime
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(REPO, "web", "data.json")
MOCK = os.path.join(REPO, "web", "insights", "mock-insights.json")

# Enough ranks to cover the zero-padding boundary (09 -> 10), which is where the
# leaderboard's rank column is most likely to show a layout problem.
ROSTER = [
    ("Aaron Judge", "NYY", "#003087", 14), ("Shohei Ohtani", "LAD", "#005A9C", 12),
    ("Juan Soto", "NYM", "#FF5910", 9), ("Kyle Schwarber", "PHI", "#E81828", 9),
    ("Pete Alonso", "NYM", "#FF5910", 8), ("Matt Olson", "ATL", "#CE1141", 8),
    ("Corbin Carroll", "ARI", "#A71930", 7), ("Bobby Witt Jr.", "KC", "#004687", 7),
    ("Gunnar Henderson", "BAL", "#DF4601", 6), ("Yordan Alvarez", "HOU", "#EB6E1F", 6),
    ("Freddie Freeman", "LAD", "#005A9C", 5),
]


def _with_signals(game, i):
    """Attach a best angle and ranked signal scores to a mock game.

    Cards.gameRow renders up to three chips from best_angle + signal_scores,
    deduped on market|side. Long market labels on purpose: they are what makes
    the strip wrap to a second line, which is the case worth rendering.
    """
    away = (game.get("away") or {}).get("abbr") or "AWY"
    home = (game.get("home") or {}).get("abbr") or "HME"
    game = dict(game)
    game["best_angle"] = {
        "market": "First Five Moneyline", "side": away,
        "bet_type": "f5_moneyline", "score": 78 - i * 4,
    }
    game["signal_scores"] = [
        {"market": "Team Total Over", "side": home, "score": 71 - i * 3},
        {"market": "Run Line", "side": away, "score": 64 - i * 2},
        {"market": "Game Total Under", "side": None, "score": 58 - i},
    ]
    return game


def build():
    with open(MOCK) as fh:
        mock = json.load(fh)

    now = (datetime.datetime.now(datetime.timezone.utc)
           .replace(microsecond=0).isoformat().replace("+00:00", "Z"))

    players = [
        {
            "rank": i + 1, "entity": name, "team": "Sample Club", "team_abbr": abbr,
            "team_color": color, "logo_path": "assets/logos/mlb/147.png",
            "value": value, "total_qualified": 142, "window": 10, "met": 8,
        }
        for i, (name, abbr, color, value) in enumerate(ROSTER)
    ]

    data = {
        "generated_at": now,
        "sports": {
            "mlb": {
                "label": "MLB",
                "categories": [
                    {"key": "hr", "label": "Home Runs", "short_label": "HR",
                     "sub": "last 10 games", "kind": "count", "players": players},
                    {"key": "rbi", "label": "RBI", "short_label": "RBI",
                     "sub": "last 10 games", "kind": "count", "players": players[:6]},
                ],
            }
        },
        # insights.js reads players/games from here; teams and components come
        # from the committed mock instead.
        "insights": {
            "players": mock["players"],
            # The committed mock predates signal scores, so its games render no
            # market chips at all -- which meant the suite could never see the
            # chip strip that only appears with real pipeline data. Synthesised
            # here so the games view under test matches what actually ships.
            "games": [_with_signals(g, i) for i, g in enumerate(mock["games"])],
            "ui": {},
        },
    }

    with open(OUT, "w") as fh:
        json.dump(data, fh, indent=1)
    return OUT


if __name__ == "__main__":
    print("wrote %s" % build())
