"""Entry point: fetch -> normalize -> rank -> write a dated Markdown report
to ./output/ with a ranked "who's hot" table per stat category."""

import base64
import datetime
import html
import os

import yaml

import normalizer
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

SPORT_LABELS = {
    "mlb": "⚾ MLB",
    "worldcup": "⚽ World Cup",
}

CATEGORY_LABELS = {}
CATEGORY_SHORT_LABELS = {}
CATEGORY_ORDER_BY_SPORT = {}

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon-180.png")


def load_icon_base64():
    with open(ICON_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def index_category_labels(config):
    for sport_key in ("mlb", "worldcup"):
        cat_keys = []
        for cat_cfg in config.get(sport_key, {}).get("stat_categories", []):
            CATEGORY_LABELS[cat_cfg["key"]] = cat_cfg.get("label", cat_cfg["key"])
            CATEGORY_SHORT_LABELS[cat_cfg["key"]] = cat_cfg.get("short_label", cat_cfg["key"])
            cat_keys.append(cat_cfg["key"])
        CATEGORY_ORDER_BY_SPORT[sport_key] = cat_keys


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
            value = f"{r['value']:.2f}" if isinstance(r["value"], float) else r["value"]
            lines.append(
                f"| {r['rank']} | {r['entity']} | {r['team'] or '-'} | {value} | {last_game} |"
            )
        lines.append("")
    return "\n".join(lines)


def format_value(value):
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def rank_badge_class(rank):
    if rank == 1:
        return "rank-1"
    if rank <= 3:
        return "rank-2-3"
    return "rank-rest"


def render_html(ranked_records, generated_at):
    by_sport_category = {}
    for r in ranked_records:
        by_sport_category.setdefault(r["sport"], {}).setdefault(r["stat_category"], []).append(r)

    sport_keys = [s for s in SPORT_LABELS if s in by_sport_category]

    sport_tabs = []
    cat_chip_groups = []
    panels = []
    for s_idx, sport_key in enumerate(sport_keys):
        sport_active = " active" if s_idx == 0 else ""
        sport_tabs.append(
            f'<button class="sport-tab{sport_active}" data-sport="{sport_key}">'
            f"{html.escape(SPORT_LABELS[sport_key])}</button>"
        )

        cat_keys = [c for c in CATEGORY_ORDER_BY_SPORT.get(sport_key, []) if c in by_sport_category[sport_key]]
        chips = []
        for c_idx, cat_key in enumerate(cat_keys):
            chip_active = " active" if s_idx == 0 and c_idx == 0 else ""
            chips.append(
                f'<button class="chip{chip_active}" data-sport="{sport_key}" data-cat="{cat_key}">'
                f"{html.escape(CATEGORY_SHORT_LABELS.get(cat_key, cat_key))}</button>"
            )
        group_hidden = "" if s_idx == 0 else " hidden"
        cat_chip_groups.append(
            f'<nav class="cat-chips" data-sport-group="{sport_key}"{group_hidden}>{"".join(chips)}</nav>'
        )

        for c_idx, cat_key in enumerate(cat_keys):
            records = sorted(by_sport_category[sport_key][cat_key], key=lambda r: r["rank"])
            panel_hidden = "" if (s_idx == 0 and c_idx == 0) else " hidden"
            rows = []
            for r in records:
                last_game = f"Last: {r['last_game_date']}" if r["last_game_date"] else "No recent game"
                rows.append(
                    f'<li class="row">'
                    f'<span class="rank-badge {rank_badge_class(r["rank"])}">{r["rank"]}</span>'
                    f'<div class="who">'
                    f'<div class="name">{html.escape(r["entity"])}</div>'
                    f'<div class="sub">{html.escape(r["team"] or "-")} &middot; {html.escape(last_game)}</div>'
                    f"</div>"
                    f'<div class="val">{format_value(r["value"])}</div>'
                    f"</li>"
                )
            panels.append(
                f'<section class="cat-panel" data-sport="{sport_key}" data-cat="{cat_key}"{panel_hidden}>'
                f'<h2>{html.escape(CATEGORY_LABELS.get(cat_key, cat_key))}</h2>'
                f'<ol class="board">{"".join(rows)}</ol>'
                f"</section>"
            )

    return HTML_TEMPLATE.format(
        generated_at=generated_at.strftime("%b %-d, %Y %-I:%M %p"),
        icon_b64=load_icon_base64(),
        sport_tabs="".join(sport_tabs),
        cat_chip_groups="".join(cat_chip_groups),
        panels="".join(panels),
    )


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Who's Hot</title>
<!-- iOS "Add to Home Screen": custom icon + full-screen (no Safari chrome) launch -->
<link rel="apple-touch-icon" href="data:image/png;base64,{icon_b64}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Who's Hot">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#eb6834">
<style>
  :root {{
    --page-plane: #f9f9f7;
    --surface-1: #fcfcfb;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --accent: #2a78d6;
    --accent-wash: rgba(42,120,214,0.14);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --page-plane: #0d0d0d;
      --surface-1: #1a1a19;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
      --accent: #3987e5;
      --accent-wash: rgba(57,135,229,0.18);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--page-plane);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .dash {{ max-width: 560px; margin: 0 auto; padding-bottom: 32px; }}
  .topbar {{
    position: sticky; top: 0; z-index: 5;
    background: var(--page-plane);
    padding: 20px 16px 8px;
    border-bottom: 1px solid var(--border);
  }}
  .topbar h1 {{ margin: 0; font-size: 22px; display: flex; align-items: center; gap: 10px; }}
  .topbar .logo {{ width: 32px; height: 32px; border-radius: 9px; display: block; }}
  .topbar .meta {{ margin-top: 4px; font-size: 13px; color: var(--text-muted); }}
  .sport-tabs {{
    display: flex; gap: 8px; padding: 12px 16px 0;
  }}
  .sport-tab {{
    flex: 1; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--surface-1); color: var(--text-secondary);
    font-size: 15px; font-weight: 600; font-family: inherit;
  }}
  .sport-tab.active {{ background: var(--accent); color: #ffffff; border-color: var(--accent); }}
  .cat-chips {{
    display: flex; gap: 8px; padding: 12px 16px 4px;
    overflow-x: auto; -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }}
  .cat-chips::-webkit-scrollbar {{ display: none; }}
  .chip {{
    flex: 0 0 auto; padding: 8px 14px; border-radius: 999px; border: 1px solid var(--border);
    background: var(--surface-1); color: var(--text-secondary);
    font-size: 13px; font-weight: 600; font-family: inherit; white-space: nowrap;
  }}
  .chip.active {{ background: var(--accent-wash); color: var(--accent); border-color: var(--accent); }}
  .cat-panel {{ padding: 8px 16px 4px; }}
  .cat-panel h2 {{
    font-size: 14px; font-weight: 600; color: var(--text-secondary);
    margin: 8px 4px 10px;
  }}
  .board {{ list-style: none; margin: 0; padding: 0; }}
  .row {{
    display: flex; align-items: center; gap: 12px;
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 10px 12px; margin-bottom: 8px;
  }}
  .rank-badge {{
    flex: 0 0 auto; width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums;
    background: var(--gridline); color: var(--text-secondary);
  }}
  .rank-badge.rank-1 {{ background: var(--accent); color: #ffffff; }}
  .rank-badge.rank-2-3 {{ background: var(--accent-wash); color: var(--accent); }}
  .who {{ flex: 1 1 auto; min-width: 0; }}
  .name {{ font-size: 15px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .sub {{ font-size: 12px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .val {{
    flex: 0 0 auto; font-size: 18px; font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  [hidden] {{ display: none !important; }}
</style>
</head>
<body>
  <div class="dash">
    <header class="topbar">
      <h1><img class="logo" src="data:image/png;base64,{icon_b64}" alt=""> Who's Hot</h1>
      <div class="meta">Generated {generated_at}</div>
    </header>
    <nav class="sport-tabs">{sport_tabs}</nav>
    {cat_chip_groups}
    <main>{panels}</main>
  </div>
  <script>
    document.querySelectorAll('.sport-tab').forEach(function (tab) {{
      tab.addEventListener('click', function () {{
        var sport = tab.dataset.sport;
        document.querySelectorAll('.sport-tab').forEach(function (t) {{ t.classList.toggle('active', t === tab); }});
        document.querySelectorAll('.cat-chips').forEach(function (group) {{
          group.hidden = group.dataset.sportGroup !== sport;
        }});
        var firstChip = document.querySelector('.chip[data-sport="' + sport + '"]');
        if (firstChip) firstChip.click();
      }});
    }});
    document.querySelectorAll('.chip').forEach(function (chip) {{
      chip.addEventListener('click', function () {{
        var sport = chip.dataset.sport, cat = chip.dataset.cat;
        document.querySelectorAll('.cat-chips[data-sport-group="' + sport + '"] .chip').forEach(function (c) {{
          c.classList.toggle('active', c === chip);
        }});
        document.querySelectorAll('.cat-panel').forEach(function (panel) {{
          panel.hidden = !(panel.dataset.sport === sport && panel.dataset.cat === cat);
        }});
      }});
    }});
  </script>
</body>
</html>
"""


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
    html_report = render_html(ranked, generated_at)

    output_dir = config.get("output_dir", "output")
    os.makedirs(output_dir, exist_ok=True)
    date_stamp = generated_at.strftime("%Y-%m-%d")

    md_path = os.path.join(output_dir, f"{date_stamp}-whos-hot.md")
    with open(md_path, "w") as f:
        f.write(markdown)

    html_path = os.path.join(output_dir, f"{date_stamp}-whos-hot.html")
    with open(html_path, "w") as f:
        f.write(html_report)

    # Stable filenames (overwritten every run) so a bookmarked/published copy
    # of the dashboard can always be refreshed in place instead of piling up
    # a new dated snapshot that lingers with stale data.
    latest_md_path = os.path.join(output_dir, "latest-whos-hot.md")
    with open(latest_md_path, "w") as f:
        f.write(markdown)

    latest_html_path = os.path.join(output_dir, "latest-whos-hot.html")
    with open(latest_html_path, "w") as f:
        f.write(html_report)

    print(f"Wrote {md_path}, {html_path}, and latest-whos-hot.{{md,html}} ({len(ranked)} ranked rows)")


if __name__ == "__main__":
    main()
