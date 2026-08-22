"""Entry point: fetch -> normalize -> rank -> write output/data.json, the
single payload the static site (index.html/app.js/app.css) fetches client
side. Also writes a Markdown snapshot as a lightweight debug artifact."""

import datetime
import json
import os

import yaml

import generate_insights
import normalizer
import team_meta
from fetchers import epl, mlb, nfl

CONFIG_PATH = "config.yaml"

# The LEADERBOARD registry ("Who's Hot"): one fetch() per sport returning raw,
# unranked player records. Deliberately distinct from
# generate_insights.GAME_BUILDERS, which registers the same sports' SCORED
# per-game pick builders -- a sport can appear in one, the other, or both, and
# the two never call into each other. nfl is in both as of this change:
# nfl.build_game_entities there (moneyline), nfl.fetch here (leaderboards).
SPORT_FETCHERS = {
    "mlb": {
        "fetch": mlb.fetch,
        "competition": lambda cfg: f"MLB Regular Season {cfg['mlb']['season']}",
    },
    "nfl": {
        "fetch": nfl.fetch,
        "competition": lambda cfg: f"NFL {cfg['nfl']['season']}",
    },
    "epl": {
        "fetch": epl.fetch,
        # No season number: the fetcher windows by rolling date rather than
        # anchoring to a season, and an EPL season spans two calendar years
        # anyway, so a single year would be misleading rather than helpful.
        "competition": lambda cfg: "Premier League",
    },
}

SPORT_LABELS = {"mlb": "MLB", "nfl": "NFL", "epl": "Premier League"}

# Which categories the redesigned UI actually surfaces, and in what order
# the stat chips appear.
APPROVED_CATEGORIES = {
    "mlb": [
        "home_runs", "hits_runs_rbi", "total_bases", "hit_rate",
        "run_producer_rate", "hit_streak", "strikeouts", "k_rate",
    ],
    # Yardage/reception boards first (the everyday "who's producing" read),
    # then the four touchdown boards. Total TDs last: it is a combination of
    # the two boards immediately before it, so it reads as their summary.
    "nfl": [
        "passing_yards", "rushing_yards", "receiving_yards", "receptions",
        "passing_tds", "rushing_tds", "receiving_tds", "total_tds",
    ],
    # Unfiltered production boards first, then the position-filtered ones:
    # attacking (Fwd/Mid), then goalkeeping.
    "epl": [
        "goals", "assists", "goal_or_assist",
        "goals_per_appearance", "shots_on_goal", "shots_total",
        "clean_sheets", "saves",
    ],
}

# Presentation metadata that isn't tied to fetch mechanics, so it stays out
# of config.yaml: `kind` drives which breakdown stats the client computes
# (count: best game/1+ games; rate: average/peak/low; streak: length/hits-
# in-span/multi-hit games), `sub` is the section-title qualifier shown next
# to the category label. `title` is a clean display label for section/detail
# headers -- config.yaml's `label` is intentionally verbose (parenthetical
# window/mode detail meant for the old Markdown/HTML report headers) and
# would duplicate `sub` if reused here.
#
# hits_runs_rbi is `kind: rate` (not `count`) even though it's a combined
# counting stat: config.yaml has `per_game: true` for it, so its ranked
# `value` is already a true per-game average (like total_bases/strikeouts),
# not a raw sum (like home_runs). The `count` breakdown formula divides
# `value` by the series length to *get* a per-game average -- doing that
# to a value that's already an average would silently double-average it.
# `rate`'s Average/Peak/Low breakdown reads `value` directly instead.
CATEGORY_META = {
    "home_runs": {"kind": "count", "sub": "Last 10 G", "title": "Home Runs"},
    "hits_runs_rbi": {"kind": "rate", "sub": "Last 10 G", "title": "H+R+RBI / G"},
    "total_bases": {"kind": "rate", "sub": "Last 10 G", "title": "Total Bases / G"},
    "strikeouts": {"kind": "rate", "sub": "Starters", "title": "Strikeouts / G"},
    "hit_streak": {"kind": "streak", "sub": "Active", "title": "Hit Streak"},
    # threshold kind: value is a rate (0..1) for ranking/bars, displayed as
    # "met/window" (e.g. 8/10) in the UI; breakdown shows rate + streaks.
    # sub states the actual bar + window so the "N/M" value reads
    # unambiguously ("13/20" = 13 of the last 20 games with 1+ hit).
    "hit_rate": {"kind": "threshold", "sub": "1+ H · Last 20 G", "title": "Hit Rate"},
    "run_producer_rate": {"kind": "threshold", "sub": "2+ H+R+RBI · Last 20 G", "title": "Run Producer Rate"},
    "k_rate": {"kind": "threshold", "sub": "6+ K · Last 10 starts", "title": "K Rate"},
    # NFL. `sub` never implies daily freshness: NFL plays weekly, so these
    # boards are genuinely static from Tuesday through Saturday even though
    # the pipeline regenerates daily. "G" is games the player actually
    # played, not calendar weeks -- a bye shifts the window rather than
    # emptying it (see config.yaml's nfl block).
    #
    # `{n}` is resolved per build by _resolve_sub to the board's REAL window
    # depth rather than hardcoding the configured 4. In week 2 no one has
    # four games yet, so the board reads "Last 2 G" and stops advertising a
    # four-game trend that does not exist; from week 4 on it settles at 4 and
    # reads exactly as a static label would.
    #
    # The four yardage/reception boards are `rate` (config marks them
    # per_game: true, so their ranked value is already a true per-game
    # average). The four TD boards are `count` -- raw window totals, which
    # is what the `count` breakdown formula expects. Same rate-vs-count
    # distinction, and the same reason for it, as MLB's hits_runs_rbi note
    # above.
    "passing_yards": {"kind": "rate", "sub": "Last {n} G", "title": "Passing Yards / G"},
    "rushing_yards": {"kind": "rate", "sub": "Last {n} G · RB", "title": "Rushing Yards / G"},
    "receiving_yards": {"kind": "rate", "sub": "Last {n} G · WR/TE", "title": "Receiving Yards / G"},
    "receptions": {"kind": "rate", "sub": "Last {n} G · WR/TE", "title": "Receptions / G"},
    "passing_tds": {"kind": "count", "sub": "Last {n} G", "title": "Passing TDs"},
    "rushing_tds": {"kind": "count", "sub": "Last {n} G", "title": "Rushing TDs"},
    "receiving_tds": {"kind": "count", "sub": "Last {n} G", "title": "Receiving TDs"},
    "total_tds": {"kind": "count", "sub": "Rush + Rec · Last {n} G", "title": "Total TDs"},
    # EPL. `sub` counts APPEARANCES ("App"), not matchdays -- rotation,
    # injury and suspension shift a player's window back rather than leaving
    # gaps in it, so "Last 5 App" is the honest unit. `{n}` resolves per build
    # to the board's real depth, same mechanism as NFL's: early in a season,
    # or for a board whose qualifiers are all newly returned, it reads lower
    # than the configured 5 rather than promising a five-match trend that
    # does not exist.
    #
    # Position qualifiers are spelled out in `sub` because they are the whole
    # reason two players on different boards are not comparable: a keeper's
    # clean sheets and a striker's goals answer different questions.
    #
    # goals_per_appearance is `rate` (config marks it per_appearance, so its
    # ranked value is already an average) -- same rate-vs-count distinction as
    # MLB's hits_runs_rbi and NFL's yardage boards. Its title says "per
    # Appearance" and never "per 90": ESPN publishes no minutes played, so a
    # 10-minute cameo and a full 90 count identically, and calling it per-90
    # would claim a normalisation the data cannot support.
    "goals": {"kind": "count", "sub": "Last {n} App", "title": "Goals"},
    "assists": {"kind": "count", "sub": "Last {n} App", "title": "Assists"},
    "goal_or_assist": {"kind": "count", "sub": "Last {n} App", "title": "Goal Contributions"},
    "goals_per_appearance": {"kind": "rate", "sub": "Last {n} App · Fwd/Mid", "title": "Goals / Appearance"},
    "shots_on_goal": {"kind": "count", "sub": "Last {n} App · Fwd/Mid", "title": "Shots on Target"},
    # Volume alongside end product. Deliberately adjacent to shots_on_goal in
    # APPROVED_CATEGORIES above so the two read as a pair on the chip row --
    # total shots is the wider number by construction (ESPN never reports more
    # on target than total; verified across 597 real player-match rows), so
    # placing it after on-target reads as "and here is everything they tried".
    "shots_total": {"kind": "count", "sub": "Last {n} App · Fwd/Mid", "title": "Total Shots"},
    "clean_sheets": {"kind": "count", "sub": "Last {n} App · GK", "title": "Clean Sheets"},
    "saves": {"kind": "count", "sub": "Last {n} App · GK", "title": "Saves"},
}

CATEGORY_SHORT_LABELS = {}
CATEGORY_UNITS = {}

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_MANIFEST_PATH = os.path.join(ASSETS_DIR, "logos", "manifest.json")

_logo_manifest = None


def load_logo_manifest():
    global _logo_manifest
    if _logo_manifest is None:
        if os.path.exists(LOGO_MANIFEST_PATH):
            with open(LOGO_MANIFEST_PATH) as f:
                _logo_manifest = json.load(f)
        else:
            _logo_manifest = {}
    return _logo_manifest


def team_logo_path(sport_key, team_name):
    """Site-relative path to a team's cached logo, served as a plain static
    file alongside index.html. None if we don't have one cached (e.g. a
    World Cup team that wasn't in the field when logos were last fetched)
    -- callers should just omit the logo in that case."""
    if not team_name:
        return None
    rel_path = load_logo_manifest().get(sport_key, {}).get(team_name)
    if not rel_path:
        return None
    if not os.path.exists(os.path.join(ASSETS_DIR, rel_path)):
        return None
    return "assets/" + rel_path


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def index_category_labels(config):
    # Driven by SPORT_FETCHERS rather than a hardcoded sport tuple, so
    # registering a fetcher is the single step that makes a sport's labels
    # resolve. The tuple this replaced was ("mlb", "worldcup"); an NFL board
    # added without touching it would have silently fallen back to raw
    # snake_case category keys as its short labels, with an empty unit.
    for sport_key in SPORT_FETCHERS:
        for cat_cfg in config.get(sport_key, {}).get("stat_categories", []):
            CATEGORY_SHORT_LABELS[cat_cfg["key"]] = cat_cfg.get("short_label", cat_cfg["key"])
            CATEGORY_UNITS[cat_cfg["key"]] = cat_cfg.get("unit", "")


def _resolve_sub(sub, records):
    """A category's `sub` label with the `{n}` placeholder resolved to the
    board's real window depth, for categories whose window can be shallower
    than its configured cap.

    NFL's boards are "last 4 games", but in week 2 nobody has played four
    games, so a literal "Last 4 G" would promise a four-game trend that does
    not exist yet. `{n}` resolves to the DEEPEST window any ranked player on
    the board actually has -- i.e. the cap genuinely in effect. It can never
    exceed the configured window (the fetcher slices to it), so mid-season
    this settles on the configured number and the label reads exactly as a
    static one would.

    Deepest, not shallowest or average, because the label describes the
    BOARD's window rather than any one player's: individual variation (a
    player back from a bye with only two games) is already shown correctly
    per player -- app.js builds its bar-chart title from that player's own
    series length, and build_data emits their `window` count alongside.

    Categories with no `{n}` in their `sub` are returned untouched, so this
    is inert for MLB and needs no per-sport special-casing.

    A category using `{n}` whose records carry no depth at all is
    structurally unreachable -- build_data skips empty categories, and any
    fetcher emitting `{n}` emits the depth with it -- but if it ever happens
    the placeholder is left visible rather than replaced by a guess, so the
    bug shows up in the UI instead of shipping a confidently wrong number.
    """
    if "{n}" not in sub:
        return sub
    depths = []
    for r in records:
        # games_window is the fetcher's own count; series length is the same
        # quantity arrived at independently, so it backstops a record that
        # carried the series but not the count.
        depth = r.get("games_window") or len(r.get("series") or [])
        if depth:
            depths.append(depth)
    if not depths:
        return sub
    return sub.replace("{n}", str(max(depths)))


def rank_records(records, top_n):
    """Group by (stat_category, window), sort by value desc, assign rank +
    total_qualified (the group's full size before truncation), and truncate
    each group to the configured top_n."""
    groups = {}
    for r in records:
        groups.setdefault((r["stat_category"], r["window"]), []).append(r)

    ranked = []
    for group in groups.values():
        # Secondary key breaks value ties (used by threshold_rate, where two
        # players can share a rate -- e.g. 8/10 and 4/5 -- and the one with
        # more games met should rank higher). Defaults to 0 for every other
        # category, leaving their pure value ordering unchanged.
        group.sort(key=lambda r: (r["value"], r.get("tiebreak") or 0), reverse=True)
        total_qualified = len(group)
        for i, r in enumerate(group[:top_n], start=1):
            r["rank"] = i
            r["total_qualified"] = total_qualified
            ranked.append(r)
    return ranked


def build_data(ranked_records, generated_at):
    """Assemble the single JSON payload the static site fetches: every
    approved category, its ranked players, and everything the leaderboard +
    detail views need to render without another network call."""
    by_sport_category = {}
    for r in ranked_records:
        if r["stat_category"] not in CATEGORY_META:
            continue
        by_sport_category.setdefault(r["sport"], {}).setdefault(r["stat_category"], []).append(r)

    sports_out = {}
    for sport_key, cats_for_sport in by_sport_category.items():
        categories_out = []
        for cat_key in APPROVED_CATEGORIES.get(sport_key, []):
            if cat_key not in cats_for_sport:
                continue
            records = sorted(cats_for_sport[cat_key], key=lambda r: r["rank"])
            meta = CATEGORY_META[cat_key]
            players_out = []
            for r in records:
                tmeta = team_meta.get_team_meta(sport_key, r["team"])
                players_out.append(
                    {
                        "rank": r["rank"],
                        "entity": r["entity"],
                        "team": r["team"],
                        "team_abbr": tmeta["abbr"] if tmeta else None,
                        "team_color": tmeta["color"] if tmeta else None,
                        "logo_path": team_logo_path(sport_key, r["team"]),
                        "position": r.get("position"),
                        "value": r["value"],
                        "last_game_date": r.get("last_game_date"),
                        "total_qualified": r.get("total_qualified"),
                        "series": r.get("series") or [],
                        "vs_next_starter": r.get("vs_next_starter"),
                        # threshold_rate: met/window drive the "8/10" display.
                        "met": r.get("met"),
                        "window": r.get("games_window"),
                    }
                )
            categories_out.append(
                {
                    "key": cat_key,
                    "label": meta["title"],
                    "short_label": CATEGORY_SHORT_LABELS.get(cat_key, cat_key),
                    "unit": CATEGORY_UNITS.get(cat_key, ""),
                    "kind": meta["kind"],
                    "sub": _resolve_sub(meta["sub"], records),
                    "players": players_out,
                }
            )
        sports_out[sport_key] = {"label": SPORT_LABELS[sport_key], "categories": categories_out}

    return {"generated_at": generated_at.isoformat(), "sports": sports_out}


def main():
    config = load_config()
    index_category_labels(config)
    top_n = config.get("top_n", 10)

    # Only build the sports listed in config's active_sports (in order); others
    # (e.g. NFL, registered but not yet listed) stay wired up but dormant.
    # Falls back to every registered fetcher if the key is absent.
    #
    # A sport must be REGISTERED in SPORT_FETCHERS as well as listed here.
    # worldcup is archived and no longer registered, so listing it would log
    # "no fetcher registered" and build nothing -- reviving it means
    # re-registering it here too, not just editing config. See docs/leagues.md.
    #
    # `active_sports` gates THE LEADERBOARDS ONLY. Its counterpart for scored
    # per-game picks is `active_game_sports`, read by
    # generate_insights._active_game_sports. The two were one key until they
    # were split, which meant listing a sport here to publish its "Who's Hot"
    # boards also switched on its betting markets. `active_game_sports` falls
    # back to this key when absent, so nothing here changed behaviour -- see
    # that function's docstring for the full resolution order.
    active_sports = config.get("active_sports") or list(SPORT_FETCHERS)

    all_normalized = []
    for sport_key in active_sports:
        sport_impl = SPORT_FETCHERS.get(sport_key)
        if not sport_impl:
            print(f"Skipping '{sport_key}': no fetcher registered in SPORT_FETCHERS")
            continue
        raw_records = sport_impl["fetch"](config)
        competition = sport_impl["competition"](config)
        all_normalized.extend(normalizer.normalize(sport_key, competition, raw_records))

    ranked = rank_records(all_normalized, top_n)

    # Enrichments run after ranking/truncation so only the players who
    # actually made a top-N board pay for the extra calls (per-game series
    # for all boards; next-opponent career matchup for hitting boards).
    mlb_ranked = [r for r in ranked if r["sport"] == "mlb"]
    mlb.enrich_with_series(mlb_ranked, config)
    mlb.enrich_with_vs_next_starter(mlb_ranked, config)

    # Timezone-aware UTC: the site's freshness indicator compares this
    # timestamp against the viewer's local clock in JS, which would misread
    # a naive (no-offset) ISO string as the viewer's own local time instead
    # of UTC.
    generated_at = datetime.datetime.now(datetime.timezone.utc)
    data = build_data(ranked, generated_at)
    # AI Insight Generator: enriches `data` with per-player and per-game insight
    # text and maintains the committed insight stores (data/insights.json,
    # data/insights.games.json) + boxscore cache. `config` is passed so the game
    # builder can reach the MLB endpoints. AI generation no-ops when the claude
    # CLI is unavailable (e.g. in CI) or ai_insights.enabled is false; the
    # deterministic game build still runs either way.
    data["aiInsightsEnabled"] = generate_insights.ai_insights_enabled(config)
    generate_insights.run(data, generated_at, config=config)

    output_dir = config.get("output_dir", "output")
    os.makedirs(output_dir, exist_ok=True)
    data_path = os.path.join(output_dir, "data.json")
    with open(data_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote {data_path} ({len(ranked)} ranked rows)")


if __name__ == "__main__":
    main()
