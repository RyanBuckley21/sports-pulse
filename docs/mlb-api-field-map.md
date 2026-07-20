# MLB Stats API — reality check for Insights

Read-only discovery against the live MLB Stats API (`https://statsapi.mlb.com/api/v1`)
on 2026-07-19, mapping the Insights schema (`docs/sports-pulse-schema.md`) to real
responses. No adapter code was written — this is a feasibility/gap check before
building Phase 3 on top of Phase 2's assumptions.

## Discovery findings

### Probable pitchers
- **Endpoint:** `/schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher,team`.
  Each game exposes `teams.away.probablePitcher` / `teams.home.probablePitcher`
  (`{id, fullName}`) when announced.
- **Lead time (measured):** both sides populated for **today and +1 day**
  (16/16, 15/15 games), still strong at **+2/+3** (~10/15 with both sides), thin
  at **+4** (1/5), and **absent from +5 onward** (0/15). So plan for **reliable
  same-/next-day probables, best-effort ~3 days out, nothing beyond ~4 days.**

### Bullpen usage
- **Not directly queryable.** There is no "bullpen ERA (7d)" field or endpoint.
- **Derived** from `/game/{gamePk}/boxscore`: `teams.<side>.pitchers` is an
  ordered list of person IDs; each `players.ID<n>.stats.pitching` has
  `inningsPitched`, `earnedRuns`, `strikeOuts`, `pitchesThrown`, and
  `gamesStarted` (**GS=1 = the starter, GS=0 = relievers**). A bullpen aggregate
  = sum the GS=0 lines across a team's recent box scores → **multiple calls per
  window**, one per game.

### Inning notation
- **Confirmed thirds notation** (not decimal): observed `"6.2"` (6⅔), `"1.1"`
  (1⅓), `"1.0"`, team total `"9.0"`. `inningsPitched` is a **string**. Any
  averaging must convert to outs first (matches the existing `mlb.py` approach).

### Player game logs
- **Endpoint:** `/people/{id}/stats?stats=gameLog&group=hitting&season=YYYY`
  (or `group=pitching`). Returns **one split per game for the requested season**
  (e.g. 86 games, `2026-03-26 … 2026-07-19`). **One season per call** — history
  is reached by iterating the `season` param.
- **Per-game splits are tagged:** each split has `date`, `opponent`, **`isHome`**,
  `isWin`, `positionsPlayed`, `game`, and a full `stat` block → home/away and
  by-opponent filtering are possible **without** a separate splits call.
- **Dedicated split endpoints also exist:**
  - Home/away (season): `stats=homeAndAway` → 2 splits (`isHome` true/false).
  - **vs pitcher hand:** `stats=statSplits&sitCodes=vl,vr` → `vs Left` / `vs Right`.
  - vs specific pitcher (career): `stats=vsPlayer&opposingPlayerId=<id>`
    (already used by `fetchers/mlb.py: get_vs_pitcher_career_line`).

### Team roster / schedule
- Roster: `/teams/{id}/roster?rosterType=active` → 26 entries, each `{person,
  position, status}` (position abbreviation, active/IL status).
- Schedule: `/schedule?sportId=1&teamId={id}&startDate&endDate`; `venue.name`,
  `gameDate` (UTC), and per-side `team` (with `abbreviation`, `teamName`) present.

## Field-by-field comparison vs `docs/sports-pulse-schema.md`

| Schema field | Real API source | Status |
|---|---|---|
| `Game.id` | `gamePk` | ✅ but **integer**, schema says string — coerce |
| `Game.away/home.abbr` | `schedule … teams.*.team.abbreviation` (needs `hydrate=team`) | ✅ |
| `Game.away/home.name` | `team.teamName` ("White Sox") — **not** `team.name` ("Chicago White Sox") | ⚠️ pick the right field |
| `Game.away/home.color` | — | ❌ not in API; from `team_meta.py` |
| `Game.start` | `gameDate` (UTC ISO only) | ⚠️ **derived** — must format to local/"ET" ourselves |
| `Game.venue` | `venue.name` | ✅ |
| `Game.headline` | — | ❌ derived/AI |
| `Game.pulse` | — | ❌ computed metric we define (no equivalent) |
| `Game.signals[]` | various (see below) | ⚠️ each is derived, not a 1:1 field |
| `Game.summary` | — | ❌ AI (Phase 3) |
| `Team.abbr/name` | team endpoint `abbreviation` / `teamName` | ✅ |
| `Team.pos`,`Player.pos` | roster `position.abbreviation` | ✅ |
| `Player.name` | `people.fullName` | ✅ |
| `Player.team` | current team `abbreviation` (people hydrate) | ✅ |
| `*.pulse/headline/summary` | — | ❌ derived/AI |

## Signal feasibility (the important reality check)

The mock signals assumed some values that **do not exist** in the Stats API:

- ❌ **wOBA** (used in 2 mock player signals) — **not in StatsAPI**. It's a
  sabermetric (Fangraphs). StatsAPI gives OBP/SLG/OPS; wOBA would have to be
  computed from components or **replaced with OPS**.
- ❌ **Chase rate**, ❌ **Hard-hit %** (mock player/game signals) — **Statcast**
  metrics, served by Baseball Savant, **not** StatsAPI. Out of scope for a
  single-source (StatsAPI) build unless we add Savant as a second source.
- ⚠️ **Bullpen ERA (Nd)** — no field; **derived** by summing reliever lines
  across recent box scores (multi-call, see above).
- ⚠️ **BABIP** — no field, but **computable** from gameLog components
  (H, HR, AB, K, SF).

Signals that **are** cleanly derivable from StatsAPI:
- ✅ OPS/AVG/HR/XBH/SB and home-or-away splits → `gameLog` (per-game `stat` +
  `isHome`), windowed to any N days.
- ✅ Run differential (Nd), record (Nd) → team `schedule` + `linescore`.
- ✅ K/9, walk rate, K rate → pitching `gameLog` (`strikeOuts`, `inningsPitched`,
  `baseOnBalls`, batters faced).
- ✅ Season series / head-to-head → `schedule` filtered to the two teams.
- ✅ Games back / standings → `/standings`.

## Takeaways for Phase 3

1. **Drop or substitute Statcast/sabermetric signals** in real data: replace
   wOBA→OPS; drop chase rate / hard-hit % unless we add Baseball Savant.
2. **Bullpen and run-diff signals are multi-call derivations**, not fields —
   budget the fetch cost (fits the existing post-rank enrichment pattern).
3. **`start` and team `name`** need explicit derivation/field-selection, not a
   straight copy.
4. `color`, `pulse`, `headline`, `summary` were always going to be
   ours/derived/AI — confirmed nothing in the feed supplies them.
5. Probable-pitcher-dependent insights should degrade gracefully **beyond ~3–4
   days out**, where probables aren't published yet.
