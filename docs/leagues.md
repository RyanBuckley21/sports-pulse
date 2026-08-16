# Sports & leagues

The site is multi-sport. Which sports actually build and appear is controlled
by two lists in `config.yaml`, one per pipeline:

```yaml
active_sports: [mlb]        # leaderboards ("Who's Hot")
# active_game_sports: [mlb] # scored per-game picks (absent -> falls back to active_sports)
```

Only keys listed in `active_sports` are fetched and written to `data.json`.
The frontend is data-driven: the MLB/World Cup–style sport toggle renders one
button per sport in `data.json`, and hides itself entirely when only one sport
is present.

### Two gates, not one

These gate genuinely different pipelines, and a sport can be in either, both,
or neither:

| key | pipeline | what it controls |
| --- | --- | --- |
| `active_sports` | `generate_stats.SPORT_FETCHERS` | Leaderboards — players ranked by raw production. Descriptive; no weights, nothing graded. |
| `active_game_sports` | `generate_insights.GAME_BUILDERS` | Scored per-game picks — Signal Scores, weights, thresholds, graded outcomes, the ledger. |

They were a single key until they were split, which meant adding a sport to
publish its leaderboards also switched on its betting markets. `active_game_sports`
**falls back to `active_sports` when the key is absent**, so existing configs
need no migration and behave exactly as before. Set it explicitly only when the
two lists need to differ — e.g. `active_sports: [mlb, nfl]` with
`active_game_sports: [mlb]` publishes NFL leaderboards while keeping NFL
betting markets off. An explicitly empty `active_game_sports: []` is honoured
as a kill switch (no scored picks at all) rather than falling back.

## Currently active

- **mlb** — MLB via the public StatsAPI (`fetchers/mlb.py`).

## Archived — World Cup (pending the 2030 cycle)

The 2026 tournament is over, and the next one is roughly four years out. The
World Cup is therefore **archived, not abandoned**: it has been *unregistered*
from the active registries so it stops occupying shared namespaces, but every
piece of its implementation is still in the repo and recoverable.

**Removed from `generate_stats.py`** (this is what "unregistered" means):

- its `SPORT_FETCHERS` entry and the `worldcup` import
- its `SPORT_LABELS` entry
- `APPROVED_CATEGORIES["worldcup"]`
- its six `CATEGORY_META` entries

That last one is the point of the exercise. `CATEGORY_META`,
`CATEGORY_SHORT_LABELS` and `CATEGORY_UNITS` are **flat dicts keyed by
category key, not namespaced by sport**, so as long as the World Cup held
`goals` / `assists` / `goal_or_assist` / `shots` / `shots_on_goal` /
`clean_sheets`, no other league could use those obvious names without
colliding. Freeing them is what lets a real soccer league (EPL) claim them.

**Kept, untouched, in the repo:**

- `fetchers/worldcup.py` — the whole ESPN soccer fetcher, including the
  `limit=1000` truncation fix. Still the reference implementation for ESPN
  soccer parsing (`classify_position`, per-match roster stat extraction, the
  two-signal clean-sheet safeguard).
- the `worldcup:` block in `config.yaml` — stat category definitions
- `WORLDCUP_TEAMS` in `team_meta.py` and the cached nation logos under
  `assets/logos/worldcup/`
- `fetch_worldcup_logos()` in `scripts/fetch_logos.py`

### Reviving it for 2030

Adding `worldcup` back to `active_sports` is **no longer sufficient on its
own** — `main()` skips any sport with no `SPORT_FETCHERS` entry and logs
"no fetcher registered". Revival means:

1. Re-add the `SPORT_FETCHERS` + `SPORT_LABELS` entries and the import.
2. Re-add `APPROVED_CATEGORIES["worldcup"]` and its `CATEGORY_META` entries —
   **checking first whether the category keys are still free**, since another
   league may have claimed them in the meantime. If so, the World Cup's
   entries need distinct keys.
3. Point `config.yaml`'s `worldcup:` block at the 2030 competition path and
   refresh `WORLDCUP_TEAMS` for the new field.
4. Add `worldcup` to `active_sports`.

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
6. **Activate** — add the league's key to `active_sports` (and, if it also has
   a scored-pick builder registered in `generate_insights.GAME_BUILDERS`, to
   `active_game_sports` — see "Two gates, not one" above). The sport toggle
   reappears automatically once there are two or more active sports.

No frontend changes are required to add a sport — `web/app.js` renders whatever
sports/categories are present in `data.json`.
