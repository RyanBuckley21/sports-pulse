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
    ("Aaron Judge", "NYY", "#003087"), ("Shohei Ohtani", "LAD", "#005A9C"),
    ("Juan Soto", "NYM", "#FF5910"), ("Kyle Schwarber", "PHI", "#E81828"),
    ("Pete Alonso", "NYM", "#FF5910"), ("Matt Olson", "ATL", "#CE1141"),
    ("Corbin Carroll", "ARI", "#A71930"), ("Bobby Witt Jr.", "KC", "#004687"),
    ("Gunnar Henderson", "BAL", "#DF4601"), ("Yordan Alvarez", "HOU", "#EB6E1F"),
    ("Freddie Freeman", "LAD", "#005A9C"),
]
# name -> (abbr, colour). Skubal is not on the hitting roster: he exists only
# to populate the one-board pitching category.
TEAMS = {name: (abbr, color) for name, abbr, color in ROSTER}
TEAMS["Tarik Skubal"] = ("DET", "#0C2340")

#: The fixture used to carry TWO count categories over one shared player list,
#: which was enough while the detail page rendered only the single category you
#: tapped in from. It is not enough now that the page lists EVERY board a player
#: qualifies on, ranks them by heat, and opens each into a per-category
#: breakdown: with two categories the ordering is barely exercised, and with no
#: `series` at all every expanded section rendered "No per-game data available
#: yet" instead of the bar chart that is most of its height.
#:
#: So the boards below mirror the real shape from generate_stats.py's
#: APPROVED_CATEGORIES / CATEGORY_META -- real keys, real `kind` values, real
#: `sub` strings -- with deliberately different orderings per board so a player's
#: rank VARIES across categories. A fixture where everyone is #1 everywhere
#: would let a broken sort pass.
#:
#: Membership is built to cover the two cases that behave differently:
#:   * Aaron Judge appears on five boards at ranks 1/2/3/4/6 -- the multi-board
#:     player the page is designed around.
#:   * Tarik Skubal appears on exactly one -- the N=1 case, which auto-expands
#:     because there is no other category for a collapsed row to defer to.
#: Every `kind` is represented (count / rate / threshold / streak) so each
#: branch of buildBreakdownRows renders under test rather than in production.
BOARDS = [
    {"key": "home_runs", "label": "Home Runs", "short_label": "HR",
     "sub": "Last 10 G", "kind": "count",
     "order": ["Shohei Ohtani", "Aaron Judge", "Kyle Schwarber", "Pete Alonso",
               "Matt Olson", "Juan Soto", "Yordan Alvarez", "Gunnar Henderson",
               "Corbin Carroll", "Bobby Witt Jr.", "Freddie Freeman"],
     "top": 14},
    {"key": "total_bases", "label": "Total Bases / G", "short_label": "TB",
     "sub": "Last 10 G", "kind": "rate",
     "order": ["Aaron Judge", "Juan Soto", "Shohei Ohtani", "Yordan Alvarez",
               "Matt Olson", "Freddie Freeman"],
     "top": 3.4},
    {"key": "hits_runs_rbi", "label": "H+R+RBI / G", "short_label": "H+R+RBI",
     "sub": "Last 10 G", "kind": "rate",
     "order": ["Bobby Witt Jr.", "Shohei Ohtani", "Juan Soto", "Aaron Judge",
               "Corbin Carroll"],
     "top": 2.9},
    {"key": "hit_rate", "label": "Hit Rate", "short_label": "HIT%",
     "sub": "1+ H \u00b7 Last 20 G", "kind": "threshold",
     "order": ["Freddie Freeman", "Bobby Witt Jr.", "Corbin Carroll",
               "Gunnar Henderson", "Juan Soto", "Aaron Judge"],
     "top": 0.90},
    {"key": "hit_streak", "label": "Hit Streak", "short_label": "STREAK",
     "sub": "Active", "kind": "streak",
     "order": ["Yordan Alvarez", "Corbin Carroll", "Aaron Judge", "Pete Alonso"],
     "top": 15},
    # Pitchers, and deliberately a board of ONE so the auto-expand case exists.
    {"key": "strikeouts", "label": "Strikeouts / G", "short_label": "K",
     "sub": "Starters", "kind": "rate",
     "order": ["Tarik Skubal"],
     "top": 9.6},
]


def _series(kind, value, n, seed):
    """Deterministic per-game series matching what each `kind` expects.

    Shapes copied from what the real enrichment emits, because renderDetail
    branches on them: threshold series are binary met/miss with a `raw` count
    for the label, streak series are per-game hit counts, and count/rate series
    are plain per-game values.
    """
    out = []
    for i in range(n):
        wobble = ((seed * 7 + i * 13) % 5) - 2
        day = "2026-07-%02d" % (i + 1)
        if kind == "threshold":
            raw = max(0, 1 + wobble + (1 if i % 3 else 0))
            out.append({"date": day, "value": 1 if raw else 0, "raw": raw})
        elif kind == "streak":
            out.append({"date": day, "value": max(1, 1 + (wobble % 3))})
        else:
            base = float(value) if value else 1.0
            v = max(0, round(base * (0.55 + ((seed + i) % 6) * 0.15), 1))
            out.append({"date": day, "value": int(v) if kind == "count" else v})
    return out


#: Attached to Aaron Judge on every HITTING board he appears on (never on
#: strikeouts, which is Skubal-only anyway) -- the SAME object each time, on
#: purpose. That mirrors fetchers/mlb.py's enrich_with_vs_next_starter, which
#: caches the next opposing starter per TEAM and the career line per (batter,
#: pitcher) pair, not per stat category -- so every hitting-category record for
#: one player carries an identical vs_next_starter. Giving every board its own
#: distinct copy here would test a scenario the real pipeline cannot produce,
#: and would hide a regression where playerVsNextStarter() started reading a
#: per-category field instead of the shared one.
VS_NEXT_STARTER = {
    "pitcher_name": "Chris Sale", "game_date": "2026-07-29",
    "hits": 3, "ab": 11, "hr": 1, "rbi": 2, "avg": ".273",
}


def _board(board):
    """One category, ranks 1..n over its own ordering of the roster."""
    n = len(board["order"])
    players = []
    for i, name in enumerate(board["order"]):
        # Values descend from the board's top so rank and value never disagree.
        if board["kind"] == "threshold":
            value = round(board["top"] - i * 0.06, 2)
            met, window = int(round(value * 20)), 20
        elif board["kind"] == "count":
            value = max(1, int(board["top"]) - i)
            met, window = None, None
        elif board["kind"] == "streak":
            value = max(1, int(board["top"]) - i * 2)
            met, window = None, None
        else:
            value = round(board["top"] - i * 0.35, 2)
            met, window = None, None
        span = 20 if board["kind"] == "threshold" else 10
        players.append({
            "rank": i + 1, "entity": name, "team": "Sample Club",
            "team_abbr": TEAMS[name][0],
            "team_color": TEAMS[name][1],
            "logo_path": "assets/logos/mlb/147.png",
            "position": "SP" if name == "Tarik Skubal" else "RF",
            "value": value, "total_qualified": 142, "window": window, "met": met,
            "series": _series(board["kind"], value, span, i + 1),
            "vs_next_starter": VS_NEXT_STARTER if name == "Aaron Judge" else None,
        })
    return {
        "key": board["key"], "label": board["label"],
        "short_label": board["short_label"], "unit": "",
        "kind": board["kind"], "sub": board["sub"], "players": players,
    }


#: The committed mock carries `summary` but no `story`, so every AI note in the
#: suite renders as a single sentence. The REAL pipeline populates both --
#: generate_insights.py asks the model for a 2-3 sentence story alongside the
#: one-line summary and passes it through to insights.players / insights.games
#: -- which means the mock alone can never exercise the AI note at the length it
#: actually ships at, and the note is the tallest thing in an expanded card.
#: Same reasoning as the synthesised signal scores below.
STORY = (
    "The road offence has been the more consistent of the two all month, and "
    "the bullpen gap is what keeps showing up in the late innings rather than "
    "anything in the starting matchup. Neither side has faced this rotation "
    "since the last series."
)


def _with_signals(game, i):
    """Attach a best angle, ranked signal scores and an AI story to a mock game.

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
    game.setdefault("story", STORY)
    return game


def _with_story(player):
    """Same gap on the players side: mock players carry summary, never story."""
    player = dict(player)
    player.setdefault("story", STORY)
    return player


def build():
    with open(MOCK) as fh:
        mock = json.load(fh)

    now = (datetime.datetime.now(datetime.timezone.utc)
           .replace(microsecond=0).isoformat().replace("+00:00", "Z"))

    data = {
        "generated_at": now,
        "sports": {
            "mlb": {"label": "MLB", "categories": [_board(b) for b in BOARDS]}
        },
        # insights.js reads players/games from here; teams and components come
        # from the committed mock instead.
        "insights": {
            "players": [_with_story(p) for p in mock["players"]],
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
