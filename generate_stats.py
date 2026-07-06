"""Entry point: fetch -> normalize -> rank -> write output/data.json, the
single payload the static site (index.html/app.js/app.css) fetches client
side. Also writes a Markdown snapshot as a lightweight debug artifact."""

import datetime
import json
import os

import yaml

import normalizer
import team_meta
from fetchers import mlb, worldcup

CONFIG_PATH = "config.yaml"

SPORT_FETCHERS = {
    "mlb": {
        "fetch": mlb.fetch,
        "competition": lambda cfg: f"MLB Regular Season {cfg['mlb']['season']}",
    },
    "worldcup": {
        "fetch": worldcup.fetch,
        "competition": lambda cfg: cfg["worldcup"]["competition"],
    },
}

SPORT_LABELS = {"mlb": "MLB", "worldcup": "World Cup"}

# Which categories the redesigned UI actually surfaces, and in what order
# the stat chips appear. config.yaml keeps a couple of categories
# (hits_runs_rbi, goal_or_assist, shots_on_goal) that aren't part of this
# UI's confirmed set -- they still get fetched/ranked like everything else,
# just not emitted into data.json.
APPROVED_CATEGORIES = {
    "mlb": ["home_runs", "rbi", "total_bases", "strikeouts", "hit_streak"],
    "worldcup": ["goals", "assists", "shots", "clean_sheets"],
}

# Presentation metadata that isn't tied to fetch mechanics, so it stays out
# of config.yaml: `kind` drives which breakdown stats the client computes
# (count: best game/1+ games; rate: average/peak/low; streak: length/hits-
# in-span/multi-hit games), `sub` is the section-title qualifier shown next
# to the category label. `title` is a clean display label for section/detail
# headers -- config.yaml's `label` is intentionally verbose (parenthetical
# window/mode detail meant for the old Markdown/HTML report headers) and
# would duplicate `sub` if reused here.
CATEGORY_META = {
    "home_runs": {"kind": "count", "sub": "Last 10 G", "title": "Home Runs"},
    "rbi": {"kind": "count", "sub": "Last 10 G", "title": "RBI"},
    "total_bases": {"kind": "rate", "sub": "Last 10 G", "title": "Total Bases / G"},
    "strikeouts": {"kind": "rate", "sub": "Starters", "title": "Strikeouts / G"},
    "hit_streak": {"kind": "streak", "sub": "Active", "title": "Hit Streak"},
    "goals": {"kind": "count", "sub": "This tournament", "title": "Goals"},
    "assists": {"kind": "count", "sub": "This tournament", "title": "Assists"},
    "shots": {"kind": "rate", "sub": "This tournament", "title": "Shots / Game"},
    "clean_sheets": {"kind": "count", "sub": "Goalkeepers", "title": "Clean Sheets"},
}

CATEGORY_LABELS = {}
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
    for sport_key in ("mlb", "worldcup"):
        for cat_cfg in config.get(sport_key, {}).get("stat_categories", []):
            CATEGORY_LABELS[cat_cfg["key"]] = cat_cfg.get("label", cat_cfg["key"])
            CATEGORY_SHORT_LABELS[cat_cfg["key"]] = cat_cfg.get("short_label", cat_cfg["key"])
            CATEGORY_UNITS[cat_cfg["key"]] = cat_cfg.get("unit", "")


def rank_records(records, top_n):
    """Group by (stat_category, window), sort by value desc, assign rank +
    total_qualified (the group's full size before truncation), and truncate
    each group to the configured top_n."""
    groups = {}
    for r in records:
        groups.setdefault((r["stat_category"], r["window"]), []).append(r)

    ranked = []
    for group in groups.values():
        group.sort(key=lambda r: r["value"], reverse=True)
        total_qualified = len(group)
        for i, r in enumerate(group[:top_n], start=1):
            r["rank"] = i
            r["total_qualified"] = total_qualified
            ranked.append(r)
    return ranked


def render_markdown(ranked_records, generated_at):
    by_category = {}
    for r in ranked_records:
        by_category.setdefault(r["stat_category"], []).append(r)

    lines = [f"# Who's Hot -- {generated_at.strftime('%Y-%m-%d')}", ""]
    for category, records in by_category.items():
        records.sort(key=lambda r: r["rank"])
        label = CATEGORY_LABELS.get(category, category)
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| Rank | Player | Team | Value | Last Game |")
        lines.append("|---|---|---|---|---|")
        for r in records:
            last_game = r["last_game_date"] or "-"
            value = f"{r['value']:.2f}" if isinstance(r["value"], float) else r["value"]
            lines.append(
                f"| {r['rank']} | {r['entity']} | {r['team'] or '-'} | {value} | {last_game} |"
            )
        lines.append("")
    return "\n".join(lines)


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
                    }
                )
            categories_out.append(
                {
                    "key": cat_key,
                    "label": meta["title"],
                    "short_label": CATEGORY_SHORT_LABELS.get(cat_key, cat_key),
                    "unit": CATEGORY_UNITS.get(cat_key, ""),
                    "kind": meta["kind"],
                    "sub": meta["sub"],
                    "players": players_out,
                }
            )
        sports_out[sport_key] = {"label": SPORT_LABELS[sport_key], "categories": categories_out}

    return {"generated_at": generated_at.isoformat(), "sports": sports_out}


def main():
    config = load_config()
    index_category_labels(config)
    top_n = config.get("top_n", 10)

    all_normalized = []
    for sport_key, sport_impl in SPORT_FETCHERS.items():
        raw_records = sport_impl["fetch"](config)
        competition = sport_impl["competition"](config)
        all_normalized.extend(normalizer.normalize(sport_key, competition, raw_records))

    ranked = rank_records(all_normalized, top_n)

    # Series enrichment runs after ranking/truncation so only the players
    # who actually made a top-N board pay for the extra per-game-log call.
    mlb_ranked = [r for r in ranked if r["sport"] == "mlb"]
    mlb.enrich_with_series(mlb_ranked, config)

    # Timezone-aware UTC: the site's freshness indicator compares this
    # timestamp against the viewer's local clock in JS, which would misread
    # a naive (no-offset) ISO string as the viewer's own local time instead
    # of UTC.
    generated_at = datetime.datetime.now(datetime.timezone.utc)
    markdown = render_markdown(ranked, generated_at)
    data = build_data(ranked, generated_at)

    output_dir = config.get("output_dir", "output")
    os.makedirs(output_dir, exist_ok=True)
    date_stamp = generated_at.strftime("%Y-%m-%d")

    md_path = os.path.join(output_dir, f"{date_stamp}-whos-hot.md")
    with open(md_path, "w") as f:
        f.write(markdown)

    latest_md_path = os.path.join(output_dir, "latest-whos-hot.md")
    with open(latest_md_path, "w") as f:
        f.write(markdown)

    data_path = os.path.join(output_dir, "data.json")
    with open(data_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote {md_path}, latest-whos-hot.md, and {data_path} ({len(ranked)} ranked rows)")


if __name__ == "__main__":
    main()
