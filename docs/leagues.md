# Sports & leagues

The site is multi-sport. Which sports actually build and appear is controlled
by two lists in `config.yaml`, one per pipeline:

```yaml
active_sports: [mlb, epl, nfl]            # leaderboards ("Who's Hot")
active_game_sports: [mlb, epl, cfb, nfl]  # scored per-game picks (absent -> falls back to active_sports)
```

Only keys listed in `active_sports` are fetched into `data.json`'s leaderboard
`sports` block. The frontend is data-driven: the header league picker
(`web/sport-state.js`) offers every league that has leaderboards **or** games
and teams -- it unions `data.json`'s `sports` keys with
`insights.ui.sport_labels`, which is what lets a games-only league like CFB be
selected at all -- and hides itself when only one league is present.

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

| sport | leaderboards | games / teams / picks | graded | fetcher |
| --- | --- | --- | --- | --- |
| **mlb** | yes | yes | yes (strict CI step) | `fetchers/mlb.py` -- MLB StatsAPI |
| **nfl** | yes | yes (moneyline) | yes | `fetchers/nfl.py` -- nflverse CSVs + ESPN scoreboard |
| **epl** | yes | yes (double chance, match result) | yes | `fetchers/epl.py` -- ESPN soccer API |
| **cfb** | no, by design | yes (moneyline) | yes | `fetchers/cfb.py` -- cfbfastR CSVs + CFBD + ESPN |

CFB has no `SPORT_FETCHERS` entry and no `cfb.stat_categories`, so it never
publishes player leaderboards; it is in `active_game_sports` only. Grading for
every sport runs through `signal_report.SPORT_ADAPTERS`
(`--sport mlb|nfl|cfb|epl`); only MLB's step in `daily-stats-and-grade.yml` is
strict, the other three are `continue-on-error` for the offseason and bye-day
reasons recorded in that workflow's header.

A registered-but-inactive sport costs nothing per run: `main()` only calls
fetchers for keys in the relevant active list, so its `fetch` is never invoked.
None is in that state today; the archived World Cup below is *unregistered*,
which is a different thing.

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

## Seasonal maintenance

Most of this repo is season-agnostic, but a promotion/relegation league is not:
its membership changes every summer, and nothing in the pipeline notices.

### EPL club table — refresh every August

`team_meta.EPL_TEAMS` is a **union of two seasons' fields (23 clubs), not a
20-club snapshot**, and that is deliberate. ESPN's `/teams` endpoint flips to
the *upcoming* season's field as soon as promotion/relegation is confirmed,
while matches inside `epl.lookback_days` (75) can still belong to the season
just finished. A strict 20-club table therefore drops branding for exactly the
clubs a summer window is still reading — relegated clubs go colourless and
crestless mid-window.

Once the 2026-27 field is final (late May, after the play-off final):

1. **Add the three promoted clubs** to `EPL_TEAMS` — name, abbreviation, primary
   kit hex. The name **must match ESPN's `displayName` byte-for-byte**: it is
   the join key for both `team_meta.get_team_meta` and the logo manifest, and a
   near-miss ("Wolves" vs "Wolverhampton Wanderers") silently yields no branding
   rather than an error.
2. **Re-run the "Fetch team logos" workflow** so the promoted clubs have crests.
   This matters more in the EPL than anywhere else in the repo: club colour is
   not identifying (six near-identical reds, six near-identical blues), so the
   crest is the primary visual identifier and colour is only an accent. A
   promoted club with no cached crest renders as a colour chip alone.
3. **Prune the three relegated clubs** — but not until `lookback_days` has
   fully cleared their final match. With a 75-day window and a late-May final
   matchday, that lands in early-to-mid August, effectively the week the new
   season starts. Pruning earlier breaks live boards; leaving them indefinitely
   just carries dead entries.

The inline `# promoted for 2026-27` / `# relegated after 2025-26` comments in
`EPL_TEAMS` mark which rows each step applies to. Keep them current — they are
the only record of which of the 23 are transitional.

Nothing here is automated, and nothing fails loudly if it is skipped: a missing
club produces a board row with no chip, no colour and no crest. Worth a calendar
reminder rather than trusting it to be noticed.

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
