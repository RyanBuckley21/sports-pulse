# Insights section

A new section of Sports Pulse that pairs MLB API data with AI-generated
summaries to **explain** the most important context for today's games, teams,
and players. The goal is explanation, **not** predicting winners.

Built in approval-gated phases; each phase waits for sign-off before the next.

## Structure (Phase 1 — scaffolding only)

Standalone static pages, kept fully separate from the main single-page app so
existing functionality is untouched. They reuse the main app's design system by
linking `../app.css` (tokens, fonts, shared components) plus a small
`insights.css` for section-specific pieces.

- `index.html` — Insights hub: intro + cards linking to the three sub-sections.
- `games.html` — Today's Games (placeholder).
- `teams.html` — Teams (placeholder).
- `players.html` — Players (placeholder).
- `insights.css` — section-only styles, built on app.css `var(--…)` tokens.
- `insights.js` — no-op stub; Phase 2 data/render entry point.

## Testing

Serve a directory that mirrors the deployed layout (`app.css`, `assets/`, and
`insights/` as siblings) and open `/insights/`. Each page is reachable and
testable by direct URL; none of them depend on the main app's JS state.

## Not yet wired

- No AI or data generation yet (Phase 2+).
- No link from the main site header/nav yet (added in a later phase).
- Not copied by the deploy workflow yet. To publish, add one line to
  `.github/workflows/deploy-pages.yml`: `cp -r web/insights site/insights`
  (done when the section is approved for going live).

## Phase roadmap

1. **Scaffolding & placeholder pages** (this phase).
2. Data layer — build `output/insights.json` from MLB API data; render it.
3. AI summaries — the "explain, don't predict" layer over that data.
4. Navigation link from the main site + deploy wiring + go live.
