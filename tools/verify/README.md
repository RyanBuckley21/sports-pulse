# Browser verification suite

```
python3 -m tools.verify.make_fixture   # once per checkout, writes web/data.json
node tools/verify/run.js               # from the repo root
```

Exits non-zero on any failure. Serves `web/` on an ephemeral port with a tiny
built-in static server, and maps `/assets/...` back to the repo root so the
served tree matches what the deploy workflow assembles (it copies `web/*` and
`assets/*` side by side into the site root).

To verify a built artefact rather than the source tree:

```
VERIFY_ROOT=/path/to/site node tools/verify/run.js
```

That runs everything against the real deployed layout with the cache-busted
`?v=` URLs in place — the only way to catch breakage that exists solely after
the workflow assembles things.

Google Fonts requests are blocked, so the suite is offline-deterministic. That
means it verifies behaviour and layout, not the webfonts themselves.

## What it covers, and why each group exists

**`insights-scope-leak-check`** — the regression guard for a bug found while
scoping `insights.css`. That file redefines four classes `app.css` already owns
(`.breakdown-label` and the three `.vs-starter-*` rules), all of which the
Who's Hot player detail renders. Once both stylesheets live in one document,
`insights.css` loads second and its versions would silently win there. Nothing
throws when this breaks — the detail view just quietly looks wrong — so the
check loads both stylesheets together and asserts the computed values with the
scope class off (must equal `app.css`) and on (must equal `insights.css`).
Reasoning about cascade specificity is exactly the thing that is easy to get
subtly wrong, so this measures rather than argues.

**`re-entry`** — `app.js` and `insights.js` are mounted repeatedly by the
router instead of being re-loaded per page. This asserts the properties that
buys and the ones it puts at risk: cache keyed by source file rather than view,
the staleness window firing only past its threshold, `unmount()` resetting the
detail view, document scroll resetting on mount, and — the two most easily
broken — the games accordion collapsing when you leave and return, and
delegated click handlers surviving repeated mounts without being lost or
double-bound.

**`router`** — hash routing, the cold-launch normalisation (including that a
*valid* saved `#/games` is still normalised to `#/`, which the unknown-hash
fallback would otherwise honour), the `#/components` exemption, symmetric
container toggling, exactly one active tab, back/forward, and the
single-document guarantee. That last one pins a value to `window` and asserts
it survives every navigation: a real page load would wipe it. Playwright's
`framenavigated` fires for same-document hash changes too, so it cannot tell
the two apart.

**`standalone`** — the home-screen declarations whose absence was the original
bug: exactly one `apple-mobile-web-app-capable`, plus `viewport-fit=cover` and
the tab bar's safe-area padding, plainly wrong on a notched device.

## What it cannot cover

`navigator.standalone` is Safari-only and iOS standalone semantics cannot be
reproduced in Chromium. The meaningful half — no navigation ever leaves the
document — is asserted above, but launching from the home screen still needs a
real device: add to home screen, launch, tap every tab, confirm no Safari chrome
appears, force-quit and relaunch, confirm the start route, and confirm fresh
data after a deploy.

## Dependencies

Playwright is resolved from `./node_modules` if present, otherwise from the
global install. This repo intentionally has no `package.json` and CI runs Python
only — making the suite runnable is deliberately separate from wiring it into
CI, which is a decision to take alongside the deploy workflow.
