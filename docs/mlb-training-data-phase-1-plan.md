# MLB training-data capture — Phase 1 implementation plan

**Status:** Proposed — pending sign-off. No code written.
**Scope:** Phase 0 (schema) + Phase 1 (daily capture pipeline) of the predictive
model initiative described in `predictive-model-scoping.md`.
**Date:** 2026-07-26
**Branch:** `claude/mlb-training-data-phase-1-6jbm1g`

Phase 1 is purely deterministic data capture. No ML, no model training, no UI.
`betting_signals.py` and `implied_total.py` are read-only for this work. Nothing
here generalizes to other sports.

---

## 0. Verification: actual `build_inputs()` output

Captured by wrapping the real `betting_signals.build_inputs` and running the
production path (`fetchers.mlb._build_one_game`) against the live slate on
2026-07-26.

```
=== game === 822950 CLE @ TB | status: Preview | start: 12:15 PM ET

=== betting_signals.build_inputs() ACTUAL OUTPUT ===
{
  "away_abbr": "CLE",
  "home_abbr": "TB",
  "away_ops": 0.614,
  "home_ops": 0.795,
  "away_bullpen": 4.5,
  "home_bullpen": 1.73,
  "away_starter_era": 2.68,
  "home_starter_era": 3.28,
  "series_away_wins": 1,
  "series_home_wins": 4
}

=== availability (computed at same callsite, NOT inside build_inputs) ===
{
  "away_probable_out": false, "home_probable_out": false,
  "away_probable_id": 800048, "home_probable_id": 656876
}

=== raw schedule fields available for keying/guards ===
{
  "gamePk": 822950, "gameDate_utc": "2026-07-26T16:15:00Z",
  "officialDate": "2026-07-26", "abstractGameState": "Preview",
  "detailedState": "Scheduled", "statusCode": "S", "gameType": "R",
  "season": "2026", "doubleHeader": "N", "gameNumber": 1
}
```

Two things this settles:

- **`build_inputs()` returns exactly 10 fields** — 2 identity, 8 numeric. All
  8 numerics are `_coerce`d floats or `None`.
- **Availability is not in `build_inputs()`.** It is computed at the callsite
  (`fetchers/mlb.py:1046-1047`) as `away_out` / `home_out` from `injured_ids`
  and passed to `score_game()` separately. The scoping doc lists availability
  flags as part of the feature set, so we capture them from the callsite
  alongside the inputs dict — with no change to `betting_signals.py`.

The outcome side was verified against the completed slate for 2026-07-25
(15 games, all `Final` / `F`):

```
{ "gamePk": 824244, "abstract": "Final", "detailed": "Final", "code": "F",
  "away_score": 3, "home_score": 2, "away_isWinner": true, "home_isWinner": false,
  "currentInning": 9, "innings_played": 9,
  "first_inning_away": 0, "first_inning_home": 0 }
```

Scores, `isWinner`, and the per-inning linescore array are all available from a
single `hydrate=linescore` schedule call per date.

---

## 1. Schema (Phase 0)

**Recommendation: two record types in two files, joined on `(gamePk, date)`** —
rather than the scoping doc's single `mlb_games.jsonl` with label columns filled
in later.

Rationale: filling labels into existing rows means rewriting the features file,
and a file that gets rewritten daily is a file that can lose or corrupt pre-game
history. Splitting them makes both steps *pure appends* — the features file is
never reopened for writing after the day it is captured, which turns "no
leakage" from a code discipline into a structural property.

> **This is a deviation from the scoping doc's sketch and needs explicit sign-off.**

### Feature row — `data/training/mlb_features.jsonl`

One row per game per day.

```json
{
  "schema_version": 1,
  "gamePk": 822950,
  "date": "2026-07-26",
  "season": "2026",
  "game_type": "R",
  "game_number": 1,
  "double_header": "N",
  "start_utc": "2026-07-26T16:15:00Z",
  "venue": "George M. Steinbrenner Field",
  "captured_at": "2026-07-26T14:02:11Z",
  "status_at_capture": {"abstract": "Preview", "detailed": "Scheduled", "code": "S"},
  "features": {
    "away_abbr": "CLE", "home_abbr": "TB",
    "away_ops": 0.614, "home_ops": 0.795,
    "away_bullpen": 4.5, "home_bullpen": 1.73,
    "away_starter_era": 2.68, "home_starter_era": 3.28,
    "series_away_wins": 1, "series_home_wins": 4
  },
  "availability": {"away_probable_out": false, "home_probable_out": false},
  "probables": {"away_id": 800048, "home_id": 656876}
}
```

`features` is the `build_inputs()` dict passed through verbatim. Everything else
is added at the callsite:

| Field | Why |
|---|---|
| `season`, `game_type` | Answers the doc's open gamePk-uniqueness question, and lets you exclude spring training (`gameType: "S"`) / postseason from training later. Free from the schedule payload. |
| `game_number`, `double_header` | Doubleheaders put two games on one date; needed to reason about them without re-fetching. |
| `start_utc` | **The leakage guard's anchor** — see §4. |
| `captured_at`, `status_at_capture` | Provenance. Makes the pre-game guarantee *auditable* after the fact rather than trusted. |
| `probables.{away,home}_id` | Pitcher identity is a strong future feature and costs nothing now — already in hand at the callsite. |
| `schema_version` | So adding a field in month 3 doesn't silently break a reader over month-1 rows. |
| `venue` | Park effects, if ever wanted. Cheap. |

### Outcome row — `data/training/mlb_outcomes.jsonl`

Written only once a game is resolved.

```json
{
  "schema_version": 1,
  "gamePk": 822950,
  "date": "2026-07-26",
  "resolved_at": "2026-07-27T14:01:03Z",
  "final_status": {"abstract": "Final", "detailed": "Final", "code": "F"},
  "away_score": 3, "home_score": 2,
  "total_runs": 5,
  "home_win": false,
  "innings_played": 9,
  "first_inning_runs": 0,
  "f5_total_runs": 2
}
```

The scoping doc says pick **one** target for Phase 2 — agreed, and that stays a
Phase 2 decision. But Phase 1 should capture enough raw outcome to derive *any*
of them, because it all comes from the same single API call:

- `home_win` → moneyline (binary classification)
- `total_runs` → game-total runs (regression)
- `first_inning_runs` → NRFI / YRFI
- `f5_total_runs` → first-five total
- `innings_played` → flags extras, which distort totals

The last three correspond to markets `betting_signals.py` already scores.

### Unresolved games

Unresolved games get **no outcome row at all** — absence *is* the label-less
state the scoping doc requires. A postponed game instead gets a marker row
(`{"gamePk", "date", "resolution": "postponed", "resolved_at"}`) so it stops
being retried indefinitely and is visibly excluded from training rather than
mysteriously blank.

---

## 2. Storage design

- **Paths:** `data/training/mlb_features.jsonl`, `data/training/mlb_outcomes.jsonl`
- **Format:** JSONL, one object per line, newline-terminated, opened in `"a"` mode only
- **Committed:** confirmed. `.gitignore` covers only `__pycache__/`, `*.pyc`,
  `output/*`, and `web/data.json`. Nothing under `data/` is ignored, so new
  files there are tracked by default.

### Confirmed: never pruned

This is the explicit architectural contrast with `data/insights.games.json`,
which *is* pruned in two places today:

- `generate_insights.py:799` — `new_store = {}  # rebuilt from today's slate -> prunes yesterday's games`
- `fetchers/mlb.py:1183` — the boxscore cache is rebuilt from `touched` each run

Both prune by *rebuilding a dict and rewriting the file*. The new module will
contain **no dict rebuild and no `"w"`-mode open** for either training file —
append is the only write verb. Worth stating as a comment at the top of the
module and as the one assertion a test should cover.

### Confirmed: `(gamePk, date)` as the row key

`gamePk` alone does appear globally unique in the MLB Stats API, but the pair
costs nothing and earns its keep in exactly the case where it matters: a
postponed game replayed later can surface under the same `gamePk` on a different
`officialDate`, and the pair keeps those as two distinct feature snapshots.

`date` comes from the schedule's `officialDate`, **not** the run's wall-clock
date — the two diverge for late games that roll past midnight ET.

### Idempotency

The pipeline can run more than once a day (`workflow_dispatch`,
`push: branches: [main]`). The writer checks whether `(gamePk, date)` is already
present before appending.

Recommended semantics: **skip if already present**, so the first (earliest,
safest) pre-game snapshot wins. The alternative — letting a later run overwrite
with a richer snapshot once a starter is announced — gains a few non-null ERA
fields but moves capture closer to first pitch. Not worth the trade, but it is a
judgment call worth confirming.

### Size

~15 games/day × ~600 bytes ≈ 9 KB/day; a full 2,430-game season ≈ 1.5 MB.
Non-issue, as the scoping doc anticipated.

---

## 3. Integration points

### Feature capture (pre-game)

At the `build_inputs()` callsite — `fetchers/mlb.py:1049-1053`, inside
`_build_one_game`.

`_build_one_game` is currently pure compute + network reads with no file I/O,
and it is called in a loop over the slate. Rather than 15 individual appends
inside it, pass a mutable accumulator in and write once:

1. `_build_one_game(..., training_rows=None)` appends one row dict to the list
   when provided. **The entity shape is not changed** — which matters, because
   the entity is passed to `_call_claude_game()` for the AI payload and an extra
   key would pollute the prompt.
2. `build_game_entities` returns the accumulated rows as a third element.
3. `generate_insights._build_game_entities` performs the file append. This
   respects the existing layering, where `fetchers/mlb.py` fetches and builds
   while `generate_insights.py` owns store I/O (`_load_store` / `_save_store`).

Only `generate_insights.py:633` changes on the caller side.
`implied_total.py:211` calls `gi._build_game_entities` (the *generate_insights*
one), whose 3-tuple return is unchanged — so **`implied_total.py` is not
touched**, as required.

Captures **every game on the slate**, not just standouts: the accumulator is
appended to unconditionally, before any `standout` / `top_market` logic runs.

### Outcome capture (post-game)

A new guarded step at the top of `generate_insights.run()`, before
`build_entities()` and before `_build_game_entities()`.

- Reads `mlb_features.jsonl`, collects `(gamePk, date)` pairs with `date < today`
  that have no outcome row and no postponed marker.
- Groups them by date and issues one `schedule?date=…&hydrate=linescore` call
  per date, via the existing `mlb._get` helper (keyless, already integrated).
- Resolves **all** unresolved past dates within a bounded lookback (suggest
  10 days), not strictly yesterday. This self-heals a missed run day and picks
  up postponed-then-replayed games for free, at no extra cost when there is
  nothing to do.
- Wrapped in the same `try/except` posture as `_build_game_entities` — a
  resolver failure must never break the daily run.

---

## 4. Leakage safeguards

### Feature side — preventing capture of a game that already started

Two independent gates, **both** must pass or the row is skipped and logged
(never written):

1. **Status gate:** `abstractGameState == "Preview"`. Verified — all 15 games on
   today's slate read `Preview` / `Scheduled` / code `S`. A started game reads
   `Live` / `In Progress`; a finished one `Final`.
2. **Clock gate:** `captured_at < start_utc`, comparing UTC instants. Verified
   that `gameDate` (`"2026-07-26T16:15:00Z"`) is present on every schedule row.

Both, not either, because each covers the other's blind spot: `statusCode` can
linger at `S` / `PW` past the posted start during a delay, while a game can flip
`Preview → Live` mid-slate on a long run. The clock gate is the hard one — a
pure timestamp comparison with no dependence on how MLB happens to be reporting
state.

Two further structural constraints:

3. **Date gate:** capture only when `officialDate == game_date` for the run. The
   feature writer has no code path that accepts a past date.
4. **No re-derivation, ever.** The new module never calls `team_side_ops`,
   `team_bullpen_era`, `pitcher_season_era`, or `season_series`. It only passes
   through the dict `build_inputs()` already returned during the pre-game run.
   There is no function in it capable of computing a feature.

### Outcome side — backfilling only for completed games

5. Requires `abstractGameState == "Final"` **and** `statusCode in {"F", "O"}`
   **and** both scores non-null. `Live`, `Postponed`, `Suspended`, `Delayed`
   write no row and are retried next run. Verified against the 2026-07-25 slate:
   15 games, all `F`, all with scores and `isWinner` populated.
6. **The outcome writer cannot write features.** Different file, different
   schema, no feature fields in its row type. Structural, not conventional.

### Auditability — the backstop

Because every feature row carries `captured_at`, `start_utc`, and
`status_at_capture`, any future training script can assert
`captured_at < start_utc` across the entire file and refuse to train if a single
row violates it. That converts the guarantee from something trusted into
something checked.

### Accepted, logged loss

The cron is `0 14 * * *` (14:00 UTC / 10:00 ET) and the earliest first pitch
today is 16:15 UTC — a comfortable ~2h margin. But early getaway-day games
(11:05 ET) and international series (London / Tokyo, ~12:00 UTC) start *before*
the run. Those games are correctly **dropped** by the clock gate rather than
captured post-start.

That is the right call — a leaked row is worse than a missing one — but it is a
real, silent sampling bias. The writer should log skips loudly, and an earlier
cron may eventually be worth considering.

---

## Open decision: persistence

**The daily workflow has no persistence path for anything it generates.**
`.github/workflows/deploy-pages.yml` runs with `permissions: contents: read`,
builds into `site/`, and deploys to Pages — it never commits back.
`_build_game_entities` genuinely does run in CI (only the AI calls are gated),
so the features *would* be computed daily at 14:00 UTC and then discarded with
the runner.

As designed, the training store therefore only grows on days the pipeline is run
locally and committed — the same way `data/insights.games.json` is maintained
today. For a store whose entire value is unbroken daily accumulation over weeks,
that is a meaningful gap.

- **(a)** Add a commit-back step to the daily workflow (`contents: write` +
  commit the two JSONL files). Reliable and unattended; the training store is
  the one artifact that warrants it. Does change the posture of a Pages workflow.
- **(b)** Keep it local-run-only, matching today's convention, and accept gaps
  on days the pipeline isn't run.

**Recommendation: (a).** The whole premise of Phase 1 is "runs silently for
weeks," and (b) quietly makes that false.

---

## Needs sign-off before implementation

1. The field list in §1 (feature row and outcome row).
2. The two-file split — a deviation from the scoping doc's single-file sketch.
3. Skip-if-present idempotency semantics (§2).
4. The persistence decision above.

## Out of scope for Phase 1

Per `predictive-model-scoping.md`: no model training, no historical backfill, no
changes to `betting_signals.py` or `implied_total.py`, no generalization to other
sports, and no UI surfacing. Phase 2's open questions (minimum data threshold,
moneyline-vs-run-total as first target, retraining cadence) are deliberately not
addressed here.
