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

## TeamRef

A compact team reference, embedded in `Game.away` / `Game.home`. Rendered by the
internal `teamTag` helper (reuses `.team-chip`).

```
TeamRef {
  abbr   : string    // required for the chip to render, e.g. "NYY"
  name?  : string    // full/nickname label (not shown in the game chip today)
  color? : string    // #RRGGBB; defaults to a neutral grey if absent
}
```

## Game

Rendered by `Cards.gameInsight`.

```
Game {
  id?      : string       // stable key; NOT currently read by the component
  away     : TeamRef
  home     : TeamRef
  start?   : string        // display time, e.g. "7:10 PM ET"
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
