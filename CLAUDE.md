# CLAUDE.md: working in sports-pulse

Read `README.md` first for what the project is and how the pipeline fits
together. This file covers **how to work here safely**: the invariants you must
not break, the conventions the owner expects, and lessons earned in previous
sessions.

## Start of every session

1. **Fetch before you trust the checkout.** Bots commit to `main` several times a
   day (grading, regenerate, training capture), and a stale clone has already
   caused one session to misdiagnose "missing" changes.
   `git fetch origin main && git log --oneline origin/main -5`.
2. **The history in a fresh container is shallow.** Run `git fetch --unshallow origin main`
   before any `git log`/`blame` archaeology. Bot commits drown out everything
   else, so filter them:
   `git log --format='%h %ad %s' --date=short | grep -vE "Regenerate stats|Grade yesterday|Capture MLB"`.
3. **The why lives in the code comments and PR bodies**, not in docs. PRs #1–#61
   carry the measurements behind each decision. Before changing a threshold,
   weight, cron or guard, read the comment block above it. It almost always
   records the incident or measurement that set it.

## Commands

```bash
pip install -r requirements.txt                  # requests, PyYAML (Python 3.11)

# Python suites: offline, deterministic, run from the repo root; each prints "N checks pass"
python3 -m tools.verify.test_game_isolation      # per-sport store freeze vs. off-day clear
python3 -m tools.verify.test_slate_dates         # Eastern slate boundary, fall-forward window
python3 -m tools.verify.test_odds                # sticky price, OFF lines, bettability, CLV
python3 -m tools.verify.test_nfl                 # NFL tiers + grading (tie = PUSH)
python3 -m tools.verify.test_cfb_signals         # CFB fallback-tier gating
python3 -m tools.verify.test_cfb_grading         # CFB grading (tie = UNRESOLVED)
python3 -m tools.verify.test_epl_grading         # draw rules: wins double_chance, loses match_result
python3 -m tools.verify.test_epl_coldstart       # EPL below MIN_MATCHES
python3 -m tools.verify.test_epl_fetch           # ESPN month-window walk; sweeps all callers for date ranges
python3 -m tools.verify.test_season_phase        # postseason/preseason picks kept out of the regular-season record
python3 -m tools.verify.test_backtest_season     # backtest_season exits 1 when it skipped every date
python3 -m tools.verify.test_mlb_odds            # MLB price join (names, doubleheaders); moneyline-only market gate
python3 -m tools.verify.test_bet_board           # Bets tab: -200 split, record matched on PRICE, 30-pick proven gate
python3 -m tools.tokens.test_colorkit

# Browser suite (Playwright; Chromium is preinstalled in the cloud container)
python3 -m tools.verify.make_fixture && node tools/verify/run.js      # expect "186 passed, 0 failed" or more

# Pipeline (hits live APIs and REWRITES COMMITTED data/ files, so restore afterwards)
python3 generate_stats.py && git checkout -- data/
python3 signal_report.py --sport mlb --date YYYY-MM-DD --no-record  # read-only grading look
python3 signal_report.py --date D --rev <sha> --no-record           # grade from a past store
```

**CI runs every suite** (`.github/workflows/tests.yml`, since 2026-09-24) on each
pull request and each push to `main`: all `tools/verify/test_*.py` and
`tools/tokens/test_*.py` found by glob, with the network blocked by
`tools/verify/offline/sitecustomize.py`, then `git diff --exit-code`, then the
browser suite. So a new suite must be **offline** (a live request fails CI) and
must **not write committed files**. Still run the relevant suites locally before
pushing, and paste the real output and exit codes; CI is the backstop, not the
first check. Locally in a proxied container, reproduce the guard with
`env -u HTTPS_PROXY -u HTTP_PROXY PYTHONPATH=tools/verify/offline python3 -m ...`,
because a loopback proxy otherwise slips past it.

## Non-negotiable invariants

Each of these fails **silently**: the run stays green and the damage shows up
days later in an append-only file. Most have a test. Don't weaken them.

1. **Signal Scores are deterministic and price-blind.** No odds, line or market
   number may flow into `signal_core`, `betting_signals`, or `<sport>_signals`.
   `espn_odds.py` is display, filtering and CLV only. A model that follows the
   line destroys the only thing its record means.
2. **No AI calls in the pipeline.** `ai_insights.enabled: false`, the
   `SP_SKIP_INSIGHTS` env var and the `claude` CLI probe are three layers kept
   deliberately. The prose-generation code was deleted, so flipping the flag
   only un-hides old stored text. Don't reintroduce `claude -p` calls.
3. **Slate dates are Eastern.** Anything that names "which day's games" goes
   through `slate_clock` (`eastern_date`, `yesterday`). Never call `.date()` on the
   UTC `generated_at`. That exact bug lost two days of grading (2026-08-27/28).
4. **The games store (`data/insights.games.json`) is the graded pre-game snapshot.**
   - Once a slate has started, don't overwrite a store that already covers that
     date (`_games_store_writable`).
   - A sport whose builder **failed** keeps its partition **frozen**. A sport with
     a genuine **off day** has its partition **cleared**. Both yield an empty slate,
     and confusing them destroys what the grader reads.
   - If every sport fails, **raise**. Never write an empty `data.json` or store over
     a good one.
   - `odds` is **sticky**: a rebuild never replaces a stored price with `None`,
     because ESPN drops lines at kickoff. `betting_signals`/`standout` *are*
     rebuilt every run.
5. **Per-sport isolation.** One feed outage must not take other sports down, in
   either `generate_stats.main` (leaderboards) or `_build_game_entities` (games).
   A sport contributes all of its rows or none of them.
6. **Append-only files stay append-only.** `data/signal_report_history.jsonl`
   and `data/training/*.jsonl` are never rewritten or reordered. Corrections are
   new rows. The ledger stores raw `observed` facts so grading rules can be
   re-applied. The ledger reader takes each date's latest `run_id`. Gaps are
   written once, as explicit `no_store`/`no_picks` rows. Training capture is
   skip-if-present: the earliest pre-game snapshot wins.
7. **Grade before regenerate** in `daily-stats-and-grade.yml`. The store holds one
   slate at a time, and regenerating first grades today's gamePks under
   yesterday's date.
8. **Calibrated numbers are measured, never guessed.** Every weight, scale and
   threshold in `config.yaml` comes from a backtest, and the derivation is
   written beside it. To change one, re-run the relevant `*_backtest.py`, then
   update the number *and* its comment with the new evidence. The
   estimate-graded MLB record (totals vs. `implied_total`) is **not** a tuning
   target (see `signal_report.py`).
9. **CFBD quota safety.** `CFB_ALLOW_CFBD=1` is set **only** in
   `daily-stats-and-grade.yml`, because only that workflow commits
   `data/boxscores.json` (the per-(season, week) cache). Setting it anywhere else,
   especially in `deploy-pages.yml`, burns roughly 1,400 of the 1,000 monthly
   free calls.
10. **`deploy-pages.yml` keeps `permissions: contents: read`.** Write access is
    scoped to the workflows that commit. The push-deploy throttle uses
    `actions/cache` precisely to avoid needing write access.
11. **ESPN rejects `dates=START-END` ranges** (HTTP 400 since 2026-09-15). Use
    `espn_dates.fetch_window` (month keys, filtered locally). `test_epl_fetch` sweeps
    every ESPN caller for range-shaped requests.
12. **Caller-contract arity.** `generate_insights._build_game_entities` returns a
    3-tuple because `implied_total.py` unpacks it. Add new outputs as
    out-parameters (the pattern `team_entities` / `failed_sports` use), not as a
    4th return value.
13. **Sport semantics are rules, not accidents.** An NFL tie is a **PUSH**. A CFB
    tie is **UNRESOLVED** (bad feed). An EPL draw **wins** `double_chance` and
    **loses** `match_result`. NFL store keys are nflverse `game_id`s, re-keyed
    from ESPN. CFB team joins go through `_team_ref` (AF/AFA, BUF/BUFF).

## Conventions the owner expects

- **Comments explain *why*, with evidence.** This codebase is densely commented
  on purpose: dates, measured numbers, incident references, and what would break
  otherwise. Match that density and tone in any code you touch. Don't strip or
  "tidy" these blocks, and when behaviour changes, update the comment's facts.
- **Measure, don't reason.** Verify claims against live feeds or real data
  before building on them, as PR #61 did when it measured ESPN's range rejection
  across nine widths. When scoping shows a feature can't work, say so; PR #61
  dropped a planned spread model after walk-forward tests.
- **Tests are sabotage-checked.** When you add a test, break the code it guards
  in each direction and confirm that exactly the expected assertions fail. Record
  this in the suite's docstring and in `tools/verify/README.md`.
- **Fixtures are real captured API data**, trimmed to the fields read and never
  hand-written. Where an edge case was never observed (postponed games), edit
  only the one field that branch reads, and document the edit.
- **No new dependencies casually.** Runtime deps are `requests` and `PyYAML`
  only. The feeds are fetched as plain CSV/JSON over `requests` on purpose
  (no nflreadpy, polars or cfbfastR). There is no `package.json`, and Playwright
  is used only by the verify suite.
- **Frontend is vanilla JS IIFEs** registering `SP.views.<name> = {mount, unmount}`
  and driven by `web/shell.js` (hash router: GitHub Pages has no SPA fallback).
  Script load order in `index.html` matters. `insights.css` is scoped under
  `body.insights-scope` because it collides with `app.css`. The league selection
  lives only in `sport-state.js`. Any new local CSS/JS file must be added to the
  deploy workflow's copy list **and** its cache-bust loop, otherwise the build fails.
- **Design tokens** are sourced in `tools/tokens/tokens.py` (audit:
  `python3 -m tools.tokens.audit`; preview: `web/tokens.html`).
- **Adding a sport** follows `docs/leagues.md`: a fetcher, entries in
  `SPORT_FETCHERS` and/or `GAME_BUILDERS`, a config block, `APPROVED_CATEGORIES`
  and `CATEGORY_META`, `team_meta`, logos, a grading adapter in
  `signal_report.SPORT_ADAPTERS`, a new `continue-on-error` grading step in the
  daily workflow, and a backtest before any weight ships. Register the sport
  first and activate it in a separate step.
- **Workflows**: every cron entry, timeout and `continue-on-error` has a
  measured reason in the file header. Only MLB's grading step is strict.

## Git / PR workflow

- Work on your `claude/...` branch. The owner (RyanBuckley21) reviews and merges.
  Don't open a PR unless asked.
- `main` moves under you (bot data commits). Before opening or updating a PR,
  **merge** `origin/main` in (don't rebase shared branches). Conflicts in
  `data/*.json(l)` almost always resolve to **main's** version, because CI owns
  those files.
- Don't commit regenerated `data/` files from a local run unless the change is
  specifically about them (e.g. a deliberate backfill; see `dd3f8a0`).
- PR bodies in this repo are long-form: the problem with its measured evidence,
  what changed, what was deliberately *not* done, and test results with check
  counts. Follow that pattern.

## Lessons from previous sessions

- **Report evidence, not verdicts** (`docs/dev-notes.md`). Paste actual output
  and exit codes. A non-zero exit stays unresolved until it's explained. Kill
  background servers **by PID** (`srv & SRV=$!; kill $SRV`), never
  `pkill -f <pattern>`, which killed its own shell once (exit 144).
- **Sabotage checks can be fooled by stale bytecode.** Python trusts a cached
  `.pyc` whose source has the same **size** and the same **mtime second**. A
  same-length sabotage (`>= 2` → `>= 1`) restored with `cp` inside one second
  leaves the sabotaged bytecode running against the restored source, so the
  restored code "fails". It happened on 2026-09-25. Run sabotage loops with
  `PYTHONDONTWRITEBYTECODE=1`, or clear `__pycache__` before trusting a
  post-restore run.
- **"Nothing threw" is the common failure mode here.** Past bugs (UTC slate
  date, `OFF` odds read as missing, EPL's nine-day outage, unreachable CFB tab,
  rest_diff suppressing NFL week-1 fallbacks) all ran green. When touching
  pipeline logic, ask "what does this do on an empty or failed input?", then test
  that.
- **GitHub's scheduler is unreliable**: runs are 3–9h late, and some days all of
  them get dropped. Design for it (redundant crons, cross-workflow alarms,
  freeze guards). Don't assume a cron fires on time.
- **Bot pushes made with `GITHUB_TOKEN` do not trigger other workflows.** That's
  why `deploy-pages.yml` also listens for `workflow_run`.
- **Hit rate ≠ edge.** The NFL picks don't beat the closing line (PR #56). CFB's
  record was inflated by −3000 favourites, hence the bettability cap of −1000
  (break-even 0.9091). Don't present accuracy as profitability.

## Known drift and open items (as of 2026-09-24)

The stale docs found in the 2026-09-24 review (leagues.md, the EPL fetcher and
`GAME_BUILDERS` docstrings, config.yaml's gate comments, `signal_core.py`,
`web/insights/README.md`) have been corrected. The pattern that produced them
is worth remembering: **a docstring written at "registered but inactive" time
goes stale the day the sport goes live.** When you activate or migrate
something, grep for the old status (`inactive`, `LEADERBOARD ONLY`,
`not graded`, `NOTHING IMPORTS`) and update it in the same change.

**`signal_core.py` is now the only copy of the Signal Score math**: MLB's
`betting_signals.py` migrated on 2026-09-24, so a change there moves all four
sports' scores, MLB's graded record included. Re-run every sport's backtest
before changing it. The MLB season backtest (`backtest_season.py`) had been
silently grading nothing since 2026-08-27, which the same change fixed; if it
ever reports "0 dates graded" with every date skipped, treat that as a bug, not
a quiet week.

Operational items to keep in mind:

- **US DST ends 2026-11-01, and the crons deliberately do NOT shift.** They are
  UTC. Shifting them +1h would cut the pre-kickoff margin for EPL (12:00 UTC
  kickoffs in winter) and gain nothing for grading: the latest late kickoff
  measured in November 2025 was 03:30 UTC, well before the 10:00 UTC run. The
  reasoning is in `daily-stats-and-grade.yml`. Don't "fix" this in the fall.
- **The EPL club table** (`team_meta.EPL_TEAMS`) was pruned to the 20 clubs
  of 2026-27 on 2026-09-24. Next due **late May 2027**: add the promoted three,
  re-run the "Fetch team logos" workflow, prune the relegated three in August.
  See `docs/leagues.md`.
- The grading drop alarm in `deploy-pages.yml` is **per sport** (since
  2026-09-24): each graded sport's ledger rows against its own schedule. Its
  sport list is written out in the workflow (it runs before `pip install`), so
  **a new graded sport must be added there too**, or it is unwatched.
- The MLB season ends in late September. Expect `no_picks`/empty-slate rows and
  quiet MLB alarms from then on, which is not a failure. Training capture has
  nothing to capture in the offseason.
