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
python3 -m tools.verify.test_slate_dates         # Eastern slate boundary, fall-forward window; empty-store grading exits
python3 -m tools.verify.test_odds                # sticky price, OFF lines, bettability, CLV
python3 -m tools.verify.test_nfl                 # NFL tiers + grading (tie = PUSH)
python3 -m tools.verify.test_cfb_signals         # CFB fallback-tier gating
python3 -m tools.verify.test_cfb_grading         # CFB grading (tie = UNRESOLVED); ESPN page limit; unabbreviated sides
python3 -m tools.verify.test_epl_grading         # draw rules: wins double_chance, loses match_result
python3 -m tools.verify.test_epl_coldstart       # EPL below MIN_MATCHES
python3 -m tools.verify.test_epl_fetch           # ESPN month-window walk; sweeps all callers for date ranges
python3 -m tools.verify.test_season_phase        # postseason/preseason picks kept out of the regular-season record
python3 -m tools.verify.test_nba                 # NBA data layer: counted-game rule, Eastern dates, grading, team_meta
python3 -m tools.verify.test_stand_down          # stood-down sport: grades what its frozen store holds, else no row, no fetch
python3 -m tools.verify.test_backtest_season     # backtest_season exits 1 when it skipped every date
python3 -m tools.verify.test_mlb_odds            # MLB price join (names, doubleheaders); moneyline-only market gate
python3 -m tools.verify.test_bet_board           # Bets tab: -200 split, record matched on PRICE, 30-pick proven gate
python3 -m tools.verify.test_caution_notes       # card cautions: NFL QB injury report, CFB early read (display only)
python3 -m tools.verify.test_cfbd_budget         # CFBD monthly budget: count survives every return, no call past it
python3 -m tools.verify.test_opponent_adjust     # CFB opponent-adjusted form (production since 09-26) + the walk-forward, paired harness
python3 -m tools.tokens.test_colorkit

# Browser suite (Playwright; Chromium is preinstalled in the cloud container)
python3 -m tools.verify.make_fixture && node tools/verify/run.js      # expect "218 passed, 0 failed" or more

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
   `data/boxscores.json` (the per-(season, week) cache), and in the manual
   `cfb-backtest.yml`, which is hard-capped per run (`--cfbd-budget`, default 60)
   and keeps what it buys with `actions/cache`, so a rerun costs 0. Setting it
   anywhere else, especially in `deploy-pages.yml`, burns roughly 1,400 of the
   1,000 monthly free calls. Since 2026-09-26 the pipeline also counts its calls per UTC month
   (`cfbd_usage` in `data/boxscores.json`) and stops at `CFBD_MONTHLY_BUDGET`
   (700), leaving 300 for the backtest and logo scripts, which it can't see.
   Every run logs `CFBD calls -- this run N, YYYY-MM so far M of a 700 budget`.
   Any CFBD work outside the pipeline must count its own calls against that
   300.
10. **`deploy-pages.yml` keeps `permissions: contents: read`.** Write access is
    scoped to the workflows that commit. The push-deploy throttle uses
    `actions/cache` precisely to avoid needing write access.
11. **ESPN rejects `dates=START-END` ranges** (HTTP 400 since 2026-09-15). Use
    `espn_dates.fetch_window` (month keys, filtered locally). `test_epl_fetch` sweeps
    every ESPN caller for range-shaped requests. **And never ask for more than
    `limit=500`** (`espn_dates.SCOREBOARD_LIMIT`): above it the college-football
    scoreboard silently returns 25 events (HTTP 200), and with no limit a month
    stops at 300. Read events through `espn_dates.checked_events`, which raises
    on a full page. `test_cfb_grading` sweeps every caller for a limit over 500.
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
  hand-written. **Never read the live `data/` stores in a test**: the bot
  rewrites them daily, so a test seeded from them passes only while a league is
  in season (`test_game_isolation` and `test_bet_board` broke on MLB's first
  off day, 2026-09-28). Capture a fixture from a named commit instead. Where an edge case was never observed (postponed games), edit
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
- **Every PR updates `CLAUDE.md` and `README.md` before it merges** (owner's
  standing request, 2026-09-26). Put the docs change in the PR itself, not in a
  follow-up, so `main` is never ahead of its docs:
  - README: new tabs, files, commands and behaviour a user or reader sees;
  - CLAUDE.md: new suites in Commands, new invariants or conventions, and the
    "Known drift and open items" section (add what the PR left open, strike
    what it closed).
  If a PR changes neither, say so in its body.

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

## Next up (set 2026-10-22 by the owner)

Three things, in the owner's words: **make all the MLB stuff inactive**,
**improve the moneyline picks with data**, and **incorporate NBA**. What is
already known about each, so a new session does not re-derive it:

### Stand MLB down — DONE 2026-10-25 (this branch)

**Decided: "inactive" means nothing at all.** MLB is out of both gates. Its
last-10-games boards would show a frozen September snapshot all winter, and it
was the site's default sport (now EPL). The few World Series picks left could
add nothing to the record's open question. The mechanics and the re-activation
steps are in `docs/leagues.md` ("Standing a sport down"). The parts that are
easy to get wrong:

- **The grader now reads the picks gate** (`signal_report.is_game_sport_active`).
  It used to read neither key. A stood-down sport grades its frozen partition
  when that partition covers the date, so the pick shipped by the last
  pre-flip run is settled, not stranded: TB @ MIL NRFI, slate 2026-10-24, was
  live on the day of the flip. On every other day it exits 0 with **no row and
  no StatsAPI call**. `test_stand_down` pins both halves. The MLB step stays
  strict on purpose, because its only remaining failure is a real pick going
  unsettled.
- **The gate has one definition**, `signal_report.configured_game_sports`.
  `generate_insights._active_game_sports` is built on it (that module imports
  `signal_report`, so the definition cannot live there).
- **Switched off in place, to be restored together with MLB:**
  `deploy-pages.yml`'s grading-alarm entry and training-capture alarm, and
  `capture-training-data.yml`'s six crons.
- **Untouched:** `betting_signals.py`, `implied_total.py`, the MLB-only scripts,
  the MLB ledger rows, `data/training/`, and the `mlb:` config block.
- **World Series, as it stood when the PR opened (2026-10-29).** Most
  reports say the Brewers won it in Game 5 on 10-28. The branch sat unmerged
  through Games 3-5, so those picks were graded on the active path (10-26 HIT,
  10-27 MISS). The frozen partition holds the Game 5 pick (slate 10-28, NRFI,
  score 65): whichever grading path runs first settles it.
- **After merge, watch the first daily run.** MLB should print `stood down ...
  nothing it shipped is due` and exit 0. If a `no_store` MLB row appears for
  any date after the one the frozen store covers, the gate is not being read
  on that path.

**Order agreed with the owner:** stand-down first, then `odds_first_seen`,
then NBA registration.

### Improve the moneyline picks with data

**Measured 2026-10-22, and the answer is no edge.** This section previously
reported NFL line movement of +7.90pp toward shipped picks with a CI clearing
zero, as the project's first evidence of edge. That number was an artefact of
the baseline, and the corrected measurement kills it.

The stored `*_ml_open` is **ESPN's own opening number**, which can predate our
first sighting of a game by days. The honest baseline is the price at the moment
the pick existed. The committed games store already answers that without any new
data: it is written several times a day, so the first commit in which a gamePk
carried BOTH a price AND a standout is what the board actually came in at.
Re-measured against that anchor (122 store commits since 09-20, 116 of 123 picks
anchored; bootstrap CIs, 4,000 resamples, seed 17, null = 0.0pp):

| sport | n | from the book's open | from OUR first capture |
| --- | --- | --- | --- |
| NFL | 28 | +7.90pp [+3.78, +12.22] | **−0.50pp [−1.69, +0.76]** |
| CFB | 80 | +1.55pp [+0.37, +2.84] | **+0.70pp [−0.10, +1.50]** |
| MLB | 8 | −0.14pp [−1.92, +1.60] | −0.25pp [−0.92, +0.40] |
| all | 116 | +2.96pp [+1.56, +4.41] | **+0.35pp [−0.31, +0.99]** |

Every interval spans zero and NFL's point estimate turns slightly negative.
`2026_05_PHI_JAX` is the mechanism in one line: the book opened JAX **+110**,
our first capture already read **−285**, and it closed **−395**. From the open
that is +32.2pp; from where the board actually came in, +5.8pp. The move was
over before the model had an opinion.

**This is the fourth positive result in this repo to evaporate under a corrected
baseline** (see the opponent adjustment's +2.1pp -> −0.2%). Treat any new
"the market agrees with us" claim as guilty until the baseline is named.

What follows from it:

- **Ship `odds_first_seen`** — a price snapshot written once and never
  overwritten, with its capture timestamp — and make the first-capture figure
  the headline CLV. Keep the book-open figure if useful, but the report must not
  present it as a model result. This is a correctness fix, not a discovery.
- **The reported "Line movement" line currently overstates by about 8pp on NFL.**
  Until the field lands, do not quote it.
- **No model change is justified by CLV yet**, and three prior negative results
  stand against reaching for one: the picks do not beat a closing line, no
  combination of current signals out-predicts a spread, and MLB's score is
  inverted by confidence (90+ band 2-5, 75-89 band 14-20, under-60 52.1%, over
  595 picks and 76 dates). That inversion remains the most interesting
  unexplained thing here, and it is a data question, not a tuning one.

### Add NBA — step 1 of 3 DONE 2026-10-31 (branch `claude/nba-groundwork`)

**Three steps, and only the first is done.**
1. **Data layer (done):** `fetchers/nba.py`, `nba_grading.py`, `NBA_TEAMS`, an
   `nba:` config block with no weights, and `test_nba`. Nothing in the
   pipeline imports any of it.
2. **Backtest, then build:** `nba_backtest.py` measures each signal's
   reliability. Then come `nba_signals.py` and `build_game_entities`, the
   `GAME_BUILDERS` entry, and the `SPORT_ADAPTERS` entry (adding
   `list_markets`/`top_market` to `nba_grading`). It is still in neither gate.
3. **Activation:** the gates, a `continue-on-error` grading step, and the
   `deploy-pages.yml` alarm list.

No weight exists anywhere for NBA. A builder can't produce a standout without
one, so it waits for the backtest instead of shipping a placeholder (invariant
8). The fixtures and the measurements below come from a separate session with
ESPN access. This sandbox's proxy blocks `site.api.espn.com`.

Measured, and pinned in `test_nba` where a test can pin it:

- **A counted game needs `season.type == 2` AND `competitions[0].type.id ==
  "1"` (STD).** Neither half is enough. Season type 2 also admits All-Star
  games (ALLSTAR, id 4: STARS/WORLD/STRIPES) and the NBA Cup final (CC, id 39).
  STD also admits preseason (67 of October 2026's 155 events). For 2025-26
  that gives 1,230 games, 30 teams, 82 each; unfiltered it gives 1,235 games
  and 33 "teams".
- **`neutralSite` is not a filter.** Five games that count are at neutral sites
  (2 Cup semifinals, Mexico City, Berlin, London), while All-Star games report
  `False`.
- **Slate dates are Eastern** (invariant 3). Most games tip after 8 PM ET and
  carry the next UTC date: the Mexico City game is `2025-11-02T02:00Z`, played
  on November 1. `nba_grading.fetch_slate` walks both days' months and keeps
  the Eastern date, rather than trusting ESPN's unmeasured `dates=` day
  boundary.
- **ESPN names a season for the year it ENDS.** 2026-27 is `season.year`
  2027. nflverse does the opposite.
- **Prices are live-only and intermittent.** This corrects the 2026-10-22 note
  that said "odds coverage 100%". ESPN strips the odds block when a game
  completes: 0 of 12 on 10-28, 0 of 170 in February 2026. Before tip it may or
  may not carry one: 10 of 10 a day ahead on 10-29, but 0 of 15 for 11-02 on
  10-31. **NBA has no price history on this endpoint.** So:
  - price capture must be sticky and close to tip;
  - a missed pre-game snapshot is gone for good;
  - `odds_first_seen` is the only CLV baseline NBA will ever have;
  - the Bets tab's NBA bands can only fill going forward.
- **Home court is about half the usual quote.** Over 1,225 true-home 2025-26
  games: +1.727 pts [+0.807, +2.626], home win rate **0.5543** (4,000
  resamples, seed 17). Including all 1,230 games gives +1.737, so dropping the
  Cup final changes nothing. The shift gets fitted by the backtest, never
  assumed at 3.

**Baselines, named before any NBA number exists:**
- **Hit rate** is judged against the break-even of the graded price.
- **CLV** is measured from `odds_first_seen` only.
- **Backtest accuracy** must beat **always-home, 0.5543** on the 1,225
  true-home games. "Always the favourite" is not available historically,
  because there are no historical prices; it can only be measured going
  forward.
- **Cold start** uses prior-season margin (`build_prior_season_margin`, which
  returns game counts so the backtest picks the floor) plus the fitted home
  shift.

## Known drift and open items (as of 2026-09-26; state refreshed 2026-10-22)

**Current graded record (2026-10-22, latest run per pick):** 928 graded rows;
123 carry a price and CLV. MLB 51.4% (306-289 on 595 picks over 76 dates), and
still inverted by confidence. CFB 72.2% (192-74 on 266), down from 78% before
the bettability cap — expected, and the point of it.

**Container note:** in a fresh cloud container `pip` on PATH is Python 3.13's
while `python3` is 3.11, so `pip install -r requirements.txt` installs where
nothing can import it. Use `python3 -m pip install -r requirements.txt`.


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

**Prices and the Bets tab (#63, #64).** Moneyline prices are captured for MLB,
NFL and CFB (not EPL: its match price is three-way). The Bets tab
(`bet_board.py`, `web/insights/bets.js`) judges each lean by the model's graded
record **at similar prices, never at similar scores**; a score-matched record
was measured to mislead (a +440 underdog read as a 65-point edge). Price
capture began 2026-09-24, so every price band starts unproven (under 30 graded
picks) and the lists sort by score until one fills. NFL cards and Bets rows
warn when a starting QB is on the injury report short of Out
(`fetchers.nfl.qb_availability_note`); that warning is **display only**.

Open, in rough priority:
- **NBA playoffs and play-in are not handled.** Their competition types were
  never captured, and the counted-game rule keeps regular season only. Capture
  them before the regular season ends in mid-April.
- **NBA team colours collide after the legibility lift:** BKN/SA (`#8C8C8C`)
  and DET/NY/PHI (`#577BC2`). This is display only. Settle it at activation,
  as EPL did with alternate colours.
- **NBA logos are not cached.** `fetch-logos.yml` has no NBA step. The
  `/teams` capture carries `logo` URLs; this is due at activation.
- ~~CLV measures from the book's opening price~~ **Measured 2026-10-22, and it
  was the whole signal.** Re-anchored to the first committed store state where a
  pick had both a price and a standout, NFL falls from +7.90pp to −0.50pp
  [−1.69, +0.76] and every sport spans zero (see *Next up*). The remaining work
  is the `odds_first_seen` field so the live report stops overstating; the
  backfill proved the method on git history and needs no ledger rewrite.
- **A QB who misses every practice doesn't move the score.** Only an official
  Out/Doubtful does (`qb_out`). Changing that is a model change: backtest it
  against nflverse's injury history first.
- **NFL price-band evidence exists historically.** `nfl_odds_backtest.py`'s
  nflverse closing lines (2015-2020) could seed the Bets tab's NFL records now.
- **EPL has no captured prices**, so no Bets rows.
- ~~CFB stats are not adjusted for opponent strength~~ **Closed 2026-09-26:**
  production CFB form is `fetchers.cfb.build_team_form_adjusted` at
  `FORM_SHRINK_GAMES` (4), picked by `cfb_opponent_backtest.py`'s pre-set rule
  (walk-forward 2023-25, k=4 AUC +0.0084, CI [+0.0003, +0.0161]), and CFB's
  weights and scales were re-derived on it (`cfb_backtest.py --form adjusted`,
  78.3% on 961 at t>=40 in-sample; the derivation is in config.yaml). The gain
  is small and **not measurable in weeks 2-5**, so the "Early read" note
  (`fetchers.cfb.early_form_note`) stays. Form and weights move together:
  adjusted PPA gaps are about half as wide as raw, so a change to one without
  re-fitting the other halves or doubles every CFB score. `test_opponent_adjust`
  pins that production and `cfb_backtest.py` use the same form.
- **CFBD revises historical PPA, silently.** The 2026-09-26 re-fit's parity run
  (raw form, #46's exact procedure) did not reproduce #46's weights: same code,
  same games, turnovers identical to four decimals, but PPA r and scales moved
  (off_ppa scale 0.1686 -> 0.1950). The only input left is the PPA CFBD serves
  for 2023-25, which appears to have changed since 2026-08-16. So any CFB
  calibration drifts under a fixed config. Re-run the refit
  (`cfb-backtest.yml`, `mode=refit`) before trusting old CFB numbers, and
  compare its raw run against the last one. Note that the workflow's cache
  pins the data it first fetched, so it will not see a later revision unless
  its cache key changes.
- `cfb_backtest.py` could not reach its weights from #59 (fallback tiers in
  `SIGNAL_SPECS`, no scale) until 2026-09-26. Fixed; the fallback tiers are
  now left out of its measurement and scored with config.yaml's own scales.
- ~~Finished CFB weeks used to reach the cache 5-6 days late~~ **Confirmed
  fixed 2026-09-27:** week 4 (played 09-26) was cached by the next morning's
  run at a cost of 2 CFBD calls (September 86 -> 88), against 5-6 days and
  about 40 calls before `fetchers.cfb._week_complete`.
- ~~The CFB graded record read 25 games a Saturday~~ **Fixed 2026-09-27.**
  `cfb_grading.fetch_slate` asked ESPN for `limit=1000`, which returns 25
  events, so every pick outside them was DEFERRED and never graded (12 of 17 on
  2026-09-26), and picks on programs without a `team_meta` abbreviation
  ("Sacramento State") fell to UNRESOLVED. 2026-09-05/12/19 were re-graded from
  their historical stores as superseding ledger runs (91 rows): 44-14 (76%) on
  58 became 76-33 (70%) on 109. The truncated sample was the prominent games,
  so the old number was biased up, and the Bets tab's CFB price bands learned
  from it. **Check** after the next CFB grading run that its row count matches
  the store's leans for that date.
- Card chips tint a side by its leading token (insights.js `sideColor`), so a
  two-word school name like "Sacramento State" gets no team colour. Display
  only; the grader's version of the same rule is fixed.
- `epl_backtest.py` still builds a `dates=START-END` URL, which ESPN has
  rejected since 2026-09-15. It is a manual script, so nothing runs it, but it
  cannot re-derive EPL's weights until it walks months.
- ~~The one-off CLV review routine fires 2026-10-22~~ **Fired; result above.**

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
- **MLB is stood down (2026-10-25), so its partition of
  `data/insights.games.json` is frozen on purpose.** It will keep holding
  whatever the last pre-flip run wrote until MLB is re-activated. Don't "clean
  it up": the grader settles that pick from it, and a stood-down sport whose
  store covers no due date costs nothing. Expect **no** MLB ledger rows on
  quiet days. That is the design (a stood-down day is not a gap), which is why
  `mlb` is out of the grading-drop alarm. Re-activate before Opening Day 2027,
  using the checklist in `docs/leagues.md`.
- (Before the stand-down) The MLB season ends in late September. Expect `no_picks`/empty-slate rows and
  quiet MLB alarms from then on, which is not a failure. Training capture has
  nothing to capture in the offseason. **An empty MLB partition no longer fails
  grading** (fixed 2026-09-28): it used to exit 2 from the strict MLB step on
  the first off day after the season (run 184, a date already graded). An empty
  store now exits clean when no games were played or the date is graded, and
  still records `no_store` and fails when games were played and nothing covers
  them.
