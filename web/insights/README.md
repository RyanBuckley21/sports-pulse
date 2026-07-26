# Insights section

A section of Sports Pulse that pairs MLB API data with AI-generated summaries to
**explain** the most important context for today's games, teams, and players. The
goal is explanation, **not** predicting winners.

Built in approval-gated phases; each phase waits for sign-off before the next.

## Structure

The standalone pages this directory used to hold (`index.html`, `games.html`,
`teams.html`, `players.html`, `components.html`) are gone. The site is now a
single-document navigation shell at `web/index.html`, and the Insights views are
routes inside it:

| Route | View | Source |
| --- | --- | --- |
| `#/games` | Today's Games | `data.json` → `insights.games` |
| `#/players` | Players | `data.json` → `insights.players` |
| `#/teams` | Teams | `mock-insights.json` (preview, not live) |
| `#/components` | Card gallery | `mock-insights.json` |

`#/components` is deliberately absent from the tab bar and reachable by direct
URL only — the same arrangement `components.html` always had. It is the one route
exempt from the shell's cold-launch normalisation, because direct URL is its only
access path.

What remains here:

- `insights.js` — the six card builders plus the section's render entry point.
  Exposes `SP.Cards` for reuse and `SP.views.insights` (`mount(view)` /
  `unmount()`) for the router to drive.
- `insights.css` — section-only styles, scoped to `body.insights-scope` so they
  cannot reach the Who's Hot section now that both stylesheets share a document.
  See the header comment in that file for why the scope sits on `<body>`.
- `mock-insights.json` — the deferred mock behind the teams and components views.

## Testing

```
python3 -m tools.verify.make_fixture
node tools/verify/run.js
```

The suite covers the section's re-entry contract (cache keyed by source file,
accordion collapsing on return, delegated handlers surviving repeated mounts) and
the `insights-scope-leak-check` regression guard. See `tools/verify/README.md`.

## Phase roadmap

1. ~~Scaffolding & placeholder pages~~ — done.
2. ~~Data layer — players and games render from the live pipeline output~~ — done.
3. AI summaries — the "explain, don't predict" layer over that data.
4. ~~Navigation from the main site + deploy wiring + go live~~ — done; the tab
   bar replaced the hub, which was retired rather than routed.
