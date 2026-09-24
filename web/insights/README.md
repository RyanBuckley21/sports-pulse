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
| `#/players` | Players | `data.json` → `insights.players`, scoped to one league |
| `#/teams` | Teams | `data.json` → `insights.teams`, scoped to one league |
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
- `mock-insights.json` — the fixture behind the `#/components` gallery only. Teams
  moved to live data (`insights.teams`) on 2026-08-01.

## League scoping

**Selecting a league means the same thing on every tab: nothing from another one
is on screen.**

All three live views render a flat array spanning whatever sports the pipeline
built that run, each row carrying its own `sport`, and none of them filtered on
it. `insights.players` is ranked by pulse and nothing else, so with mlb and epl
both live it interleaved a Premier League goalkeeper into a column of MLB
hitters; Games and Teams meanwhile kept showing the MLB slate no matter which
league you picked. All three now filter, and all three render the shared sport
picker (`../sport-state.js`) above their list. That picker and its selection are
the same ones Who's Hot's header carries: pick a league on either tab and the
other agrees when you get there.

An **untagged** row is kept rather than dropped. The tag is what makes scoping
possible, and a payload without it is single-league by construction — the
committed mock behind the dev views is exactly that, and its games and teams
carry no `sport` at all.

Every league in the picker can now fill Games and Teams: all four registered
builders are in `active_game_sports` (`[mlb, epl, cfb, nfl]`). This paragraph
used to say scoping could only ever EMPTY those tabs, because
`_active_game_sports` then resolved to `[mlb]`. That stopped being true as each
league went live.

An empty Games or Teams tab is still possible, and still gets a named empty
state rather than another league's slate: an EPL international break wider
than the 14-day lookahead (observed 2026-09-24, PR #61), a genuine offseason,
or a league whose builder failed this run. Hiding tabs per
league remains an option if that emptiness ever proves annoying. That would be a
change to the shell's tab bar, not to these views.

The route notes in `../shell.js` name no league for the same reason — "Today's
MLB slate" above an empty Premier League Games tab is the same bug one line
higher up the page.

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
