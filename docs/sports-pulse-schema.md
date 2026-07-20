# Sports Pulse — Insights data schema

The shape consumed by the Phase 2 Insights UI (`web/insights/mock-insights.json`
→ the six card builders in `web/insights/insights.js`). This is the **contract**
the components read; it is independent of any mock values and of any data source.

Types are described as annotated shapes. `?` marks an optional field (the
component renders without it). All colors are `#RRGGBB` strings. All `value`
fields in signals are **pre-formatted display strings**, not numbers.

---

## Top-level document

```
InsightsDocument {
  generated_at : string      // ISO-8601 UTC, e.g. "2026-07-19T12:00:00Z"
  note?        : string      // freeform provenance note
  games        : Game[]
  teams        : Team[]
  players      : Player[]
}
```

## PulseScore

A 0–100 "how notable is this right now" gauge. Rendered by `Cards.pulseScore`.

```
PulseScore {
  score : integer   // clamped to 0..100 at render; defaults to 0 if absent
  label : string    // short caption, e.g. shown uppercased; defaults to "Pulse"
}
```

## Signal

One row in a Key Signals list. Rendered by `Cards.keySignals`.

```
Signal {
  label : string                      // metric name, e.g. "NYY road OPS (14d)"
  value : string                      // PRE-FORMATTED display value, e.g. ".812"
  tone  : "pos" | "neg" | "neutral"   // connotation (not raw direction);
                                      //   drives marker ▲/▼/• and its color.
                                      //   anything not "pos"/"neg" == "neutral"
}
```

### Signal catalog (MLB Stats API only)

`Signal` is generic, but real data must draw from **StatsAPI-native** metrics.
Baseball Savant / Statcast is **deferred** (same status as betting odds — worth
revisiting, out of scope now), so Statcast and sabermetric-only stats are **not**
part of the catalog.

Supported (derivable from StatsAPI — see `docs/mlb-api-field-map.md`):
- Rate/production: **OPS**, AVG, OBP, SLG, HR, XBH, SB, RBI (per-game logs;
  windowable to N days; splittable home/away via `isHome`).
- **BABIP** — computed from game-log components (H, HR, AB, K, SF).
- Pitching: K/9, K rate, walk rate, **WHIP**, opponent AVG (pitching game logs).
- Team: run differential (Nd), record (Nd), **bullpen ERA** (derived by summing
  reliever lines across recent box scores), head-to-head series, games back.

Excluded (deferred with Baseball Savant, do **not** use):
- ❌ **wOBA** — sabermetric, not in StatsAPI → use **OPS** instead.
- ❌ **Chase rate**, ❌ **Hard-hit %** (and other Statcast plate-discipline /
  batted-ball metrics).

## TeamRef

A compact team reference, embedded in `Game.away` / `Game.home`. Rendered by the
internal `teamTag` helper (reuses `.team-chip`).

```
TeamRef {
  abbr   : string    // required for the chip to render, e.g. "NYY"
  name?  : string    // team nickname; maps to API team.teamName ("Yankees"),
                     //   NOT team.name ("New York Yankees"). Not shown in the
                     //   game chip today (used by Team cards / future use).
  color? : string    // #RRGGBB; defaults to a neutral grey if absent
}
```

## ProbablePitcher

A game's announced starting pitcher (for `Game.probables.away/home`). Sourced
from `schedule … probablePitcher` (`{id, fullName}`); `throws`/`era` need an
extra `people` lookup and are optional in v1.

```
ProbablePitcher {
  name   : string    // probablePitcher.fullName, e.g. "Yoshinobu Yamamoto"
  throws?: string    // "L" | "R" handedness (people.pitchHand.code; extra lookup)
  era?   : string    // optional PRE-FORMATTED recent/season ERA, e.g. "3.20"
}
```

## Game

Rendered by `Cards.gameInsight`. Extended in the real-Games phase with
`probables`; `away`/`home`/`start`/`venue`/`signals`/`pulse`/`summary` unchanged
in shape.

```
Game {
  id?       : integer      // API gamePk (integer). Stable key; NOT currently
                          //   read by the component. If a producer carries it as
                          //   a string, coerce to integer at the source.
  away      : TeamRef
  home      : TeamRef
  start?    : string       // DERIVED display string, e.g. "7:10 PM ET". API gives
                          //   only gameDate (UTC ISO); the producer converts using
                          //   the VENUE timezone (/venues hydrate=timezone -> tz +
                          //   offsetAtGameTime, DST-correct). Not a raw field.
  venue?    : string       // API venue.name, e.g. "Fenway Park"
  probables?: {            // announced starters; see ProbablePitcher. Reliably
    away? : ProbablePitcher //   present same-/next-day, best-effort ~3 days out,
    home? : ProbablePitcher //   absent beyond ~4 days -> omit sides when unannounced.
  }
  headline? : string       // one-line hook (AI/derived)
  pulse?    : PulseScore    // computed metric (no API equivalent)
  signals?  : Signal[]      // Key Signals -- see "Game signal catalog (v1)" below
  summary?  : string        // AI summary text
}
```

### Game signal catalog (v1)

`Game.signals[]` uses the generic `Signal` shape; these are the StatsAPI-derived
families validated for v1 (see `docs/mlb-api-field-map.md`, Game discovery). A
card shows ~3; the producer picks the most relevant per matchup. Each `value` is
a pre-formatted string; `tone` is connotation for the side the card frames.

Core (recommended default set):
- **Home/away OPS (14d)** — a team's offense form on the relevant side. From
  `/teams/{id}/stats?stats=gameLog&group=hitting`: window 14d, filter `isHome`,
  then **sum components (AB, H, BB, HBP, SF, TB) and recompute OPS** — do NOT
  average per-game OPS. Cost: **1 call/team**. e.g. `"NYY road OPS (14d)" → ".812"`.
- **Bullpen ERA (7d)** — relief fatigue; **true bullpen only, starters excluded**.
  Sum each team's **GS=0** reliever lines (`earnedRuns`, `inningsPitched`→outs)
  across its final games in the window; ERA = `9 * ER / (outs/3)`. **Multi-call +
  cached** (see producer notes). e.g. `"BOS bullpen ERA (7d)" → "5.60" tone:"neg"`.
- **Season series** — head-to-head record this season, from
  `/schedule?teamId=X&opponentId=Y` (filters server-side), counting `isWinner`.
  Cost: **1 call**. e.g. `"Season series" → "NYY 6-4" tone:"neutral"`.
- **Probable starter ERA** — the pitching matchup, from `Game.probables` (each
  starter's ERA). e.g. `"Probables ERA" → "2.11 vs 4.05"`.

Also validated / available if a card wants them (same sources): HR/9 (Nd),
last-10 record, runs/game (Nd), run differential (Nd), games back (`/standings`).

Excluded: same Statcast/sabermetric exclusions as the main Signal catalog (no
wOBA, chase rate, hard-hit %, pitch pace).

### Producer notes — boxscore cache (NOT part of the payload)

Bullpen ERA (7d) is the only multi-call signal. A **final** game's boxscore never
changes, so cache per `gamePk`, mirroring how `data/insights.json` caches player
insight text:
- **Committed store** (survives the ephemeral container + CI), keyed by `gamePk`.
  Recommended: store only the **derived reliever line** per final game
  (`{er, ip_outs}`), not the full boxscore — same re-fetch savings, far leaner repo.
- **Only fetch boxscores for gamePks not already cached; never re-fetch a cached
  (final) game.** In-progress / scheduled games are not cached (incomplete) and
  don't contribute to a completed-games bullpen window.

This is runtime/producer guidance, not part of the `InsightsDocument` shape.

## Team

Rendered by `Cards.teamInsight`.

```
Team {
  abbr     : string
  name     : string        // shown next to the chip
  color?   : string        // #RRGGBB
  headline?: string
  pulse?   : PulseScore
  signals? : Signal[]
  summary? : string
}
```

## Player

Rendered by `Cards.playerInsight`.

```
Player {
  name     : string
  team?    : string        // team ABBREVIATION string (not a TeamRef object)
  pos?     : string        // position abbreviation, e.g. "RF", "SP"
  color?   : string        // #RRGGBB
  headline?: string
  pulse?   : PulseScore
  signals? : Signal[]
  summary? : string
}
```

---

## Notes / intentional asymmetries

- **`Game` teams are `TeamRef` objects; `Player.team` is a bare abbreviation
  string.** Games need home/away identity + color for the matchup chip; the
  player card only needs the parent-club abbreviation.
- **`Signal.value` is a formatted string**, so any numeric formatting (`.812`,
  `5.60`, `NYY 6-4`, `+22`) is the producer's responsibility, not the card's.
- **`color` is never a stat** — it comes from the app's `team_meta.py` brand
  table, not from any feed.
- **`pulse.score`, `headline`, `summary` are derived/generated**, not raw source
  fields (see `docs/mlb-api-field-map.md`).
- **`Game.start` is derived** (UTC `gameDate` → local display via the venue
  timezone) and **`Game.id` is the integer `gamePk`** — neither is a straight
  copy from the mock's earlier string values.
- **`Game.probables` may be absent** for games more than ~3–4 days out (starters
  not yet announced); omit the missing side and degrade gracefully.
- **Bullpen ERA (7d) is the one multi-call, cached signal** (per-`gamePk` boxscore
  aggregation of GS=0 reliever lines); every other game signal is a single call.
  See "Game signal catalog (v1)" + "Producer notes — boxscore cache".
- **Signals are StatsAPI-only** (see the Signal catalog above); wOBA/Statcast
  metrics are excluded pending a future Baseball Savant source.
