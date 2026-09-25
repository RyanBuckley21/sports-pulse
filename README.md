# Sports Pulse

A static, multi-sport "who's hot" dashboard with deterministic per-game betting
signals, and a ledger that grades every one of them in public.

**Live:** <https://ryanbuckley21.github.io/sports-pulse/> (GitHub Pages, installable
to the iOS home screen as a standalone app titled "Who's Hot").

The whole thing is a Python pipeline that pulls from free, keyless public feeds
(one free-tier key for college football), writes a single `data.json`, and ships
it next to a vanilla-JS single-page app. It has no backend and no database, and it
makes no AI calls at runtime. GitHub Actions runs everything on a schedule and
commits its own state back to the repo.

---

## What it shows

| Tab | What it is | Source in `data.json` |
| --- | --- | --- |
| **Who's Hot** (`#/`) | Player leaderboards ranked by raw production over a rolling window (last 10 G, last 20 G, active streaks, …). Descriptive only: no weights and nothing graded. | `sports.<sport>.categories` |
| **Games** (`#/games`) | Today's slate per league: a 0–100 **Pulse** ("how notable"), key signals, and **Signal Scores**, which give a 0–100 conviction toward a named side for each market. The strongest market that clears the bar is the game's **standout** (the pick). Where a price exists it shows the price, break-even, spread and line movement. | `insights.games` |
| **Players** (`#/players`) | Top players by Pulse, with signals and matchup angles. | `insights.players` |
| **Teams** (`#/teams`) | Team Pulse profiles built from the same slate. | `insights.teams` |

A league picker in the header scopes every tab to one sport (`web/sport-state.js`).
`#/components` is a hidden card gallery that renders `web/insights/mock-insights.json`.

## Sports

Controlled by two independent keys in `config.yaml`:

| Key | Pipeline | Current value |
| --- | --- | --- |
| `active_sports` | Leaderboards: `generate_stats.SPORT_FETCHERS` | `[mlb, epl, nfl]` |
| `active_game_sports` | Scored picks: `generate_insights.GAME_BUILDERS` | `[mlb, epl, cfb, nfl]` |

| Sport | Leaderboards | Games / Teams / picks | Graded | Data sources |
| --- | --- | --- | --- | --- |
| **MLB** | ✅ | ✅ 6 markets: moneyline, run line, game total, first-five ML, NRFI/YRFI, team total | ✅ | MLB StatsAPI (`statsapi.mlb.com`) |
| **NFL** | ✅ | ✅ moneyline | ✅ | nflverse CSV releases, plus the ESPN scoreboard for results and odds |
| **CFB** (FBS) | ❌ (by design) | ✅ moneyline | ✅ | cfbfastR schedule CSVs, CollegeFootballData (CFBD, keyed), and the ESPN scoreboard |
| **EPL** | ✅ | ✅ double chance, 3-way match result | ✅ | ESPN soccer site API |
| World Cup | archived until 2030 | none | none | ESPN (`fetchers/worldcup.py`, unregistered) |

Every Signal Score weight, scale and threshold in `config.yaml` was **calibrated
from a backtest against real completed seasons**, and the derivation is written
next to each value. Nothing is tuned by eye.

---

## How it works

```
                 ┌────────────────────────── generate_stats.py (entry point) ─────────────────────────┐
config.yaml ───▶ │ SPORT_FETCHERS[sport].fetch()  → normalizer.normalize() → rank_records() → build_data│
                 │        (isolated per sport: one feed down never takes the others with it)         │
                 │                                   │                                                │
                 │                    generate_insights.run(data)                                     │
                 │   ├─ training_capture.resolve_outcomes()      (label yesterday's MLB games)        │
                 │   ├─ GAME_BUILDERS[sport](config, date, cache) (isolated per sport)                │
                 │   │     fetchers/<sport>.build_game_entities → <sport>_signals.score_game          │
                 │   │     → signal_core (shared tanh-lean math) → standout + Team Pulse              │
                 │   │     → espn_odds price capture (NFL, CFB, MLB) + bettability (NFL, CFB)          │
                 │   ├─ training_capture.capture_features()      (append-only MLB pre-game rows)      │
                 │   └─ merge committed stores → data["insights"] {players, games, teams, ui}         │
                 └──────────────────────────────────────┬──────────────────────────────────────────────┘
                                                        ▼
                     output/data.json  +  data/insights.games.json  +  data/boxscores.json
                                                        │
          next day:  signal_report.py --sport <s>  ─────┴──▶ grades yesterday's standouts
                                                             → data/signal_report_history.jsonl
```

### Key ideas

- **Pulse** (`pulse.py`) is one shared 0–100 vocabulary: Scorching ≥85, Hot ≥70,
  Warm ≥55, Notable ≥30, Cold. Players, games and teams compute their score
  differently but all name it the same way.
- **Signal Score.** Every signal is a home-minus-away gap squashed through `tanh`
  by a measured scale. The signals are combined by market-specific weights into a
  lean, and at least two signals must agree before a side is named. Below
  `min_threshold` a market reports "No clear lean". The shared math lives in
  `signal_core.py`; each sport keeps its own wiring in `<sport>_signals.py`
  (MLB's is `betting_signals.py`).
- **Cold-start tiers.** NFL, CFB and EPL fall back to schedule-derived margins
  (this season, then last season) when calibrated form doesn't exist yet, as in
  week 1 or the start of a new EPL season. A fallback is never mixed into a
  calibrated lean.
- **Prices are for display only.** `espn_odds.py` captures the pre-kickoff line
  for NFL, CFB and MLB and keeps it ("sticky price", since ESPN drops odds at
  kickoff). It records closing-line value, and prices **moneyline picks only**:
  an MLB run line, team total or first-five pick has no price rather than the
  wrong one. For NFL and CFB it also filters out picks too short to bet (worse
  than −1000, or `OFF`); MLB has no measured cap, so it gets none. **No price
  ever feeds back into a Signal Score.**
- **The games store is a pre-game snapshot.** Once a slate has started, a late
  run can't overwrite the committed snapshot the grader reads. A failed builder
  freezes its own sport's partition, and an off day clears it.
- **Slate dates are US/Eastern** (`slate_clock.py`), never `.date()` on a UTC
  timestamp.

---

## Running locally

Requires Python 3.11 and `pip install -r requirements.txt` (`requests`, `PyYAML`).

```bash
python3 generate_stats.py            # live fetch → output/data.json (+ updates data/*.json stores)
python3 signal_report.py             # grade yesterday's MLB standouts (appends to the ledger)
python3 signal_report.py --sport cfb --date 2026-09-20 --no-record   # read-only look at any date
python3 capture_training_data.py     # standalone MLB training capture
```

> ⚠️ `generate_stats.py` **rewrites committed files** (`data/insights.games.json`,
> `data/insights.json`, `data/boxscores.json`, `data/training/*`). CI owns those
> files. Run `git checkout -- data/` afterwards unless you meant to change them.

Useful environment variables:

| Var | Effect |
| --- | --- |
| `SP_SKIP_INSIGHTS=1` | Belt-and-braces "no AI calls" (the AI prose path is already removed and disabled in config). |
| `CFBD_API_KEY` | CollegeFootballData key. Free tier is **1,000 calls/month**. |
| `CFB_ALLOW_CFBD=1` | Allows CFBD network calls. **Only the daily workflow sets this**, because only it commits the cache. Leave it unset locally so you don't burn quota. |

**Preview the site** (mirrors what `deploy-pages.yml` assembles):

```bash
mkdir -p site && cp web/*.html web/*.css web/*.js site/ && cp -r web/insights assets site/ \
  && cp output/data.json site/data.json && (cd site && python3 -m http.server 8000)
```

Or run offline against a fixture: `python3 -m tools.verify.make_fixture` writes
`web/data.json` (gitignored).

## Tests

No pytest and no `package.json`. Each suite is a plain module that prints its
check count and exits non-zero on failure. All of them are offline and
deterministic, and most use **real captured API payloads** as fixtures.

```bash
# Python pipeline suites (run from the repo root)
for f in tools/verify/test_*.py tools/tokens/test_*.py; do
  m=${f%.py}; python3 -m ${m//\//.} || echo "FAILED: $m"
done

# Browser suite (Playwright + Chromium)
python3 -m tools.verify.make_fixture && node tools/verify/run.js
```

See [`tools/verify/README.md`](tools/verify/README.md) for what each suite guards
and why. The **Tests** workflow (`.github/workflows/tests.yml`) runs all of them
on every pull request and push to `main`, with the network blocked so a suite
that starts calling a live API fails instead of flaking. Run the ones your
change touches locally first anyway.

---

## Automation (`.github/workflows/`)

| Workflow | When (UTC) | What | Writes |
| --- | --- | --- | --- |
| `daily-stats-and-grade.yml` | 10:00, 12:00, 13:40, 15:40 | **Grades first** (MLB strict; EPL, CFB and NFL `continue-on-error`), **then** regenerates | ledger, `insights*.json`, `boxscores.json` |
| `capture-training-data.yml` | 01:20–11:20, every 2h | MLB pre-game features and post-game outcomes | `data/training/` |
| `deploy-pages.yml` | 14:00, on push to `main`, and after each daily run | Builds `data.json` and deploys Pages. Also hosts the "missed day" alarms. | nothing (`contents: read`) |
| `fetch-logos.yml` | manual | Caches team logos into `assets/logos/` | `assets/logos/` |
| `tests.yml` | pull requests, push to `main` | Every Python suite (network blocked), a clean-tree check, then the browser suite | nothing (`contents: read`) |

GitHub's scheduler is routinely hours late and sometimes drops runs. That's why
there are several redundant cron entries and cross-workflow alarms, and why
several invariants are enforced in code. The workflow headers record the measured
reasons. Cron times are fixed in UTC and are deliberately **not** shifted when US
DST ends. The measured reasons are in `daily-stats-and-grade.yml`.

Secrets: `CFBD_API_KEY` (optional; CFB runs keyless on committed cache or the ESPN fallback).

---

## Repository layout

```
generate_stats.py        entry point: leaderboards + calls generate_insights.run
generate_insights.py     game/team builder orchestration, store protection, data["insights"]
signal_report.py         grading CLI + append-only ledger; per-sport SPORT_ADAPTERS
{epl,cfb,nfl}_grading.py grading adapters for signal_report
betting_signals.py       MLB Signal Scores     {nfl,cfb,epl}_signals.py  other sports
signal_core.py           shared lean math      pulse.py                  Pulse band ladder
implied_total.py         MLB run estimate (display + estimate-graded totals)
espn_odds.py             price capture, bettability filter, CLV
espn_dates.py            ESPN month-window fetch (ESPN stopped accepting date ranges 2026-09-15)
slate_clock.py           Eastern slate boundary, kickoff labels, fall-forward window
training_capture.py      MLB append-only feature/outcome stores
team_meta.py             abbreviations + brand colours (legibility-lifted)
normalizer.py            common leaderboard record schema
fetchers/                mlb, nfl, cfb, epl (+ archived worldcup)
*_backtest.py            standalone calibration/backtests (never touch the live ledger)
nfl_odds_backtest.py     NFL picks vs the closing line
mlb_estimate_calibration.py  implied_total vs actual runs
web/                     index.html shell, app.js (Who's Hot), insights/ (Games/Players/Teams)
tools/verify/            test suites + real-data fixtures
tools/tokens/            design-token source of truth, contrast audit, preview renderer
scripts/                 fetch_logos.py, gen_cfb_teams.py
data/                    committed stores, ledger, training data, backtest outputs
docs/                    schema, league how-to, MLB API field maps, dev notes
```

## Docs

- [`docs/leagues.md`](docs/leagues.md): the two sport gates, adding a league, reviving the World Cup, and the **annual EPL club-table refresh**
- [`docs/sports-pulse-schema.md`](docs/sports-pulse-schema.md): the Insights data contract the cards read
- [`docs/mlb-api-field-map.md`](docs/mlb-api-field-map.md), [`docs/mlb-availability-field-map.md`](docs/mlb-availability-field-map.md): StatsAPI reality checks
- [`docs/mlb-training-data-phase-1-plan.md`](docs/mlb-training-data-phase-1-plan.md): the training-data capture design
- [`docs/dev-notes.md`](docs/dev-notes.md): process lessons
- [`CLAUDE.md`](CLAUDE.md): working agreement for AI coding sessions in this repo

## Honest caveats

The ledger records hit rates, and a hit rate is not an edge. `nfl_odds_backtest.py`
found that the NFL picks **do not beat the closing line**, and a walk-forward
spread model on these inputs came in below break-even. The site explains games
and records its own accuracy. It does not claim to beat the market.
