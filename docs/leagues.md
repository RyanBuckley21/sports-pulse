# Sports & leagues

The site is multi-sport. Which sports actually build and appear is controlled
by one list in `config.yaml`:

```yaml
active_sports: [mlb]
```

Only keys listed here are fetched and written to `data.json`. The frontend is
data-driven: the MLB/World Cup–style sport toggle renders one button per sport
in `data.json`, and hides itself entirely when only one sport is present.

## Currently active

- **mlb** — MLB via the public StatsAPI (`fetchers/mlb.py`).

## Preserved (inactive) — World Cup

The World Cup was removed from the live site on 2026-07-19 but its full
implementation is **kept intact as a working template for soccer leagues**
(Premier League, etc.). Nothing was deleted — it's simply not in
`active_sports`. What's preserved:

- `fetchers/worldcup.py` — an ESPN soccer fetcher (scoreboard + per-match
  summary parsing, position classification, per-player stat summing). The
  Premier League uses the **same ESPN JSON shape**, just a different
  competition path (`soccer/eng.1` instead of `soccer/fifa.world`), so this is
  the natural starting point for an EPL fetcher.
- `worldcup:` block in `config.yaml` (marked INACTIVE) — the soccer stat
  category definitions (goals, assists, goal involvements, shots, shots on
  goal, clean sheets) with their `tournament_total` mode.
- `APPROVED_CATEGORIES["worldcup"]` and the `CATEGORY_META` soccer entries in
  `generate_stats.py`.
- `WORLDCUP_TEAMS` (nation colors/abbreviations) in `team_meta.py` and the
  cached nation logos under `assets/logos/`.

To bring the World Cup back: add `worldcup` to `active_sports`.

## Adding a new league (e.g. Premier League)

1. **Fetcher** — add `fetchers/<league>.py` exposing `fetch(config)` that
   returns raw records in the shape `normalizer.normalize` expects. For a
   soccer league, start from `fetchers/worldcup.py` (swap the ESPN competition
   path and adjust the "window" concept — a full league season is a rolling
   window, not a tournament-to-date total like the World Cup).
2. **Register it** in `generate_stats.py`: add an entry to `SPORT_FETCHERS`
   (with its `fetch` + `competition` label) and to `SPORT_LABELS`.
3. **Config** — add a `<league>:` block in `config.yaml` with its
   `stat_categories` (endpoints, fields, modes).
4. **Presentation** — add the league's category order to
   `APPROVED_CATEGORIES` and per-category `CATEGORY_META` (kind/sub/title) in
   `generate_stats.py`.
5. **Branding** — add the clubs' colors/abbreviations to `team_meta.py` and
   cache their logos into `assets/logos/` (+ `manifest.json`). Dark brand
   colors are auto-lifted for legibility by `team_meta._ensure_legible`.
6. **Activate** — add the league's key to `active_sports`. The sport toggle
   reappears automatically once there are two or more active sports.

No frontend changes are required to add a sport — `web/app.js` renders whatever
sports/categories are present in `data.json`.
