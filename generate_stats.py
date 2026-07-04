"""Entry point: fetch -> normalize -> rank -> write a dated Markdown report
to ./output/ with a ranked "who's hot" table per stat category."""

import datetime
import os

import yaml

import normalizer
from fetchers import mlb

CONFIG_PATH = "config.yaml"

SPORT_FETCHERS = {
    "mlb": {
        "fetch": mlb.fetch,
        "competition": lambda cfg: f"MLB Regular Season {cfg['mlb']['season']}",
    },
}

CATEGORY_LABELS = {}


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def index_category_labels(config):
    for sport_key in ("mlb", "worldcup"):
        for cat_cfg in config.get(sport_key, {}).get("stat_categories", []):
            CATEGORY_LABELS[cat_cfg["key"]] = cat_cfg.get("label", cat_cfg["key"])


def rank_records(records, top_n):
    """Group by (stat_category, window), sort by value desc, assign rank,
    and truncate each group to the configured top_n."""
    groups = {}
    for r in records:
        groups.setdefault((r["stat_category"], r["window"]), []).append(r)

    ranked = []
    for group in groups.values():
        group.sort(key=lambda r: r["value"], reverse=True)
        for i, r in enumerate(group[:top_n], start=1):
            r["rank"] = i
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
            lines.append(
                f"| {r['rank']} | {r['entity']} | {r['team'] or '-'} | {r['value']} | {last_game} |"
            )
        lines.append("")
    return "\n".join(lines)


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

    generated_at = datetime.datetime.now()
    markdown = render_markdown(ranked, generated_at)

    output_dir = config.get("output_dir", "output")
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{generated_at.strftime('%Y-%m-%d')}-whos-hot.md")
    with open(out_path, "w") as f:
        f.write(markdown)

    print(f"Wrote {out_path} ({len(ranked)} ranked rows)")


if __name__ == "__main__":
    main()
