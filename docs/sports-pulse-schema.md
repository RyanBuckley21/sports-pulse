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

## Game

Rendered by `Cards.gameInsight`.

```
Game {
  id?      : integer       // API gamePk (integer). Stable key; NOT currently
                          //   read by the component. If a producer carries it
                          //   as a string, coerce to integer at the source.
  away     : TeamRef
  home     : TeamRef
  start?   : string        // DERIVED display string, e.g. "7:10 PM ET". The API
                          //   supplies only gameDate (UTC ISO); the producer
                          //   converts it to local/ET for display. Not a raw field.
  venue?   : string        // e.g. "Fenway Park"
  headline?: string        // one-line hook
  pulse?   : PulseScore
  signals? : Signal[]
  summary? : string        // AI summary text
}
```

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
- **`Game.start` is derived** (UTC `gameDate` → local/ET display) and **`Game.id`
  is the integer `gamePk`** — neither is a straight copy from the mock's earlier
  string values.
- **Signals are StatsAPI-only** (see the Signal catalog above); wOBA/Statcast
  metrics are excluded pending a future Baseball Savant source.
