# MLB Stats API — player availability reality check

Read-only discovery against the live MLB Stats API (`https://statsapi.mlb.com/api/v1`)
on 2026-07-20 (15-game slate), mapping **player availability** (roster / injured-list
status) to real responses. No adapter code was written and no schema was changed — this
is a feasibility/gap check before any availability signal is scoped onto the Team/Game
entities, in the same spirit as `docs/mlb-api-field-map.md` (Phase 2) and the Game
discovery notes in that file.

## Discovery findings

### Roster status endpoint
- **Endpoint:** `/teams/{id}/roster?rosterType={type}`. One call returns **every listed
  player's status** for that team — `person{id, fullName}`, `position{abbreviation}`,
  `status{code, description}`, `jerseyNumber`, `parentTeamId`. Adding `hydrate=person`
  inlines bio fields but **no injury description and no day-to-day flag**.
- **`rosterType` options (measured entry counts for one club):**

  | rosterType | entries | contains |
  |---|---|---|
  | `active` | 26 | the **available** MLB roster — every entry is `A` (Active); IL players are simply absent |
  | `40Man` | 40 | 40-man roster: `A` + short-IL (`D7/D10/D15`) + `RM` (reassigned to minors). **60-day IL drops off here.** |
  | `fullRoster` | 272 | everything incl. `D60`, `ILF` (full-season), `RA` (rehab), `RST`, `NYR`, `DEV` |
  | `depthChart` | 48 | active + short-IL, grouped by position |

- **`date=YYYY-MM-DD` param supported** — roster snapshots can be queried as-of a date
  (confirmed: today vs 10-days-ago returned different rosters). A morning-generated build
  reflects all moves through that morning.

### Status-code catalog
Observed `status.code` → `status.description` across teams:

| code | description | availability meaning |
|---|---|---|
| `A` | Active | **available** |
| `D7` / `D10` / `D15` | Injured 7/10/15-Day | **short IL — out now** |
| `D60` | Injured 60-Day | long IL (off the 40-man) |
| `ILF` | Injured - Full Season | out for the year |
| `RA` | Rehab Assignment | injured, on a minor-league rehab stint |
| `RM` | Reassigned to Minors | not on the active MLB roster |
| `RST` | Restricted List | unavailable (non-injury) |
| `NYR` | Not Yet Reported | not yet available |
| `DEV` | Development List | not available |

There is **no "day-to-day" code.** A day-to-day player stays `A` (Active); day-to-day
status is **not** in this feed.

### Transactions endpoint (the "what changed and when" layer)
- **Endpoint:** `/transactions?teamId={id}&startDate&endDate` (or league-wide, no
  `teamId`). Dated move log: `date`, `effectiveDate`, `resolutionDate`,
  `typeCode`/`typeDesc`, `person`, `description`.
- **Relevant `typeCode`s:** `SC` Status Change (IL placement / activation), `ASG` rehab
  assignment, `OPT` / `CU` option / recall, `DES`, `SGN`.

## Currency / lag
- **Same-day.** League-wide transactions **dated today** returned **156 entries** with
  `effectiveDate = today` — signings, assignments, and status changes post the day they're
  processed. IL placements and reinstatements (`SC`) appear the same day the club's move is
  official (minutes-to-hours after announcement, not next-day).
- **IL placements are often backdated** — `effectiveDate` can precede the announcement
  `date` (standard MLB retroactive dating). Use `effectiveDate` for "out since when,"
  `date` for "when it hit the wire."
- Roster snapshots reflect the current state; for a dashboard generated each morning, the
  roster call is current as of that build.

## Queryability & call cost
Availability is **fully per-team in a single call** — no per-player lookups. `rosterType`
on one club returns all statuses at once; building `person_id → status` and the IL set
(`D*` / `ILF`) is a local dict comprehension. Cross-referencing a leaderboard entity or a
probable starter against that set is a **0-call** local lookup.

| Question | Source | Call cost | Lag |
|---|---|---|---|
| Who's available today | `roster?rosterType=active` | 1 / team | same-day |
| Who's on short IL | `roster?rosterType=40Man` (`D7/D10/D15`) | 1 / team | same-day (often backdated) |
| Who's on long IL | `roster?rosterType=fullRoster` (`D60/ILF`) | 1 / team | same-day |
| When a move happened | `transactions` | 1 / team-range | same-day, `effectiveDate` may be retro |
| Is *this* player out | local `id`→status lookup | 0 | — |

## Field-by-field comparison (availability onto Team/Game entities)

| Need | Real API source | Status |
|---|---|---|
| Player available / on IL (binary) | `roster … status.code` (`A` vs `D*`/`ILF`) | ✅ one call per team |
| Which IL (7/10/15/60-day) | `status.code` (`D7/D10/D15/D60`) | ✅ |
| Rehab in progress | `status.code` = `RA` + `transactions` `ASG` | ✅ |
| Probable starter on IL right now | `Game.probables.*` pitcher `id` → team status map | ✅ 0-call cross-ref |
| Team's best hitter on IL right now | leaderboard `entity_id` → team status map | ✅ 0-call cross-ref |
| When the move happened / effective date | `transactions` (`date`, `effectiveDate`) | ✅ |
| **Day-to-day / game-time decision** | — | ❌ not in StatsAPI (see below) |
| **Injury description / severity / body part** | — | ❌ not in StatsAPI (see below) |
| **Expected return date / probable-to-play %** | — | ❌ not in StatsAPI (see below) |

### Maps cleanly onto existing entities
- This is **already half-built in the pipeline.** `fetchers/mlb.py: get_roster_index()`
  already fetches `40Man` for every team and builds an `injured` set from `D*` codes (it's
  how injured players are filtered off the leaderboards today). Availability enrichment for
  Team/Game entities would **reuse that exact pass** — no new endpoint, no new call cost.
- **Probable starter on IL?** Cross-ref `Game.probables.{away,home}` pitcher `id` against
  the team status map. Verified against today's slate (probable read `A`). An announced
  probable is active by definition, but the check is one local lookup.
- **Team's best hitter on IL?** The leaderboard already carries `entity_id`; look it up in
  the team status map → available vs `D*`. Same dict, any `person_id`.

## Recommendation

**Use `active` + `40Man` only (2 calls/team). Do not use `fullRoster` for this signal.**

- `active` tells you who **is** available; `40Man` adds the short-IL cases
  (`D7`/`D10`/`D15`) — exactly the players who could otherwise be on a "hot" board or in a
  probable-starter slot and suddenly be out.
- **`fullRoster`'s extra coverage isn't needed here.** The only cases it adds beyond
  `40Man` are `D60` / `ILF` — players who have been out 60+ days or are done for the season.
  By definition they are **not** plausible candidates for a "hot" leaderboard signal or a
  probable-starter announcement, so paying the third call per team buys no relevant
  coverage for this use case.
- Net: **2 calls/team** (dedupable across the ~30-team slate), all local cross-referencing
  after that, and it reuses the roster pass the pipeline already makes.

## Deferred / unavailable data category

**StatsAPI gives HARD availability only** — a player is `A` (active), on a dated IL
(`D7/D10/D15/D60`, `ILF`), on a rehab assignment (`RA`), or otherwise off the active
roster (`RM/RST/NYR/DEV`). That's a **binary-ish, official-roster** signal.

It does **NOT** expose:
- ❌ **Day-to-day / game-time-decision / questionable** status — a DTD player stays `A`.
- ❌ **Injury description, severity, or body part.**
- ❌ **Expected return date** or a **probability-to-play**.

This is a **deferred / unavailable data category, not a bug** — the same status as
**Baseball Savant / Statcast** and **betting odds / lines** elsewhere in these docs
(see `docs/mlb-api-field-map.md` → "Decision"). Those richer availability signals live in
news / injury-report feeds, not the Stats API. Any availability feature built on StatsAPI
should be scoped to hard availability (active / short-IL / rehab) and degrade gracefully
where day-to-day nuance would be needed — revisit only if a second (news/injury) source is
added, exactly like the Savant and odds deferrals.

## Takeaways for a future availability signal
1. **Roster status is a one-call-per-team, same-day signal** — reuse the existing
   `get_roster_index` pass; add `active` alongside the `40Man` it already fetches.
2. **`active` + `40Man` only** — short-IL is the relevant window; skip `fullRoster`.
3. **Cross-referencing is free** — probable-starter and leaderboard-entity IL checks are
   local `id`→status lookups, zero extra calls.
4. **Hard availability only** — no day-to-day / severity / return-date; flag and degrade
   gracefully, deferred like Statcast and odds.
5. **`transactions`** is the optional "since when / what changed" layer (`effectiveDate`
   may be backdated) if a move-timeline is ever wanted.
