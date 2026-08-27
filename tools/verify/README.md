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

**`game-only-league`** — a league that publishes GAMES AND TEAMS BUT NO PLAYER
LEADERBOARDS. `cfb` is exactly that by design (no player props to bet, so no
player boards are built), and it broke the app in two ways that a normal run
never surfaces. The sport picker was derived from `data.json`'s `sports` block,
which only holds leagues WITH leaderboards — so cfb's games and teams shipped in
the payload, correctly scoped, with no control anywhere in the app that could
select them. Nothing threw; the league was simply unreachable. And once it *is*
selectable, walking from Games back to Who's Hot handed `renderChipRow` an
undefined sport and threw. This splices real cfb rows into whatever payload the
suite is serving and walks Games → Teams → Who's Hot and back, asserting both
halves: the league is offered and scopes both tabs, and Who's Hot names it
rather than crashing — without quietly resetting the selection, which would undo
a switch made one tab over.

Sabotage-checked in both directions when written: dropping the
`insights.ui.sport_labels` union fails exactly the "picker offers it" assertion
(and then cannot proceed); removing the no-leaderboards branch in `app.js`
fails exactly the three Who's Hot assertions, with the page both rendering the
PREVIOUS league's boards and throwing.

`game_only_league_fixture.json` is REAL pipeline output — a captured 2025-11-15
cfb slate, four games and six team profiles, exactly as `generate_insights`
emits them. Real rather than hand-written for the same reason the EPL fixture
below is: the shape of a row from a league with no leaderboards behind it is the
thing under test. The suite tags the served payload's own rows with its first
league on the way past, because `scoped()` deliberately keeps an UNTAGGED row
and a two-league payload with half its rows untagged is not one the pipeline can
produce.

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

## Pipeline tests (Python, no browser)

```
python3 -m tools.verify.test_game_isolation   # from the repo root
```

**`test_game_isolation`** — per-sport isolation in
`generate_insights._build_game_entities`, and specifically the pair of
behaviours that look identical from outside: a sport whose BUILDER FAILED has
its committed store partition frozen, while a sport having a genuine OFF DAY
still has its partition cleared. Both produce an empty slate, both log a similar
line, and getting them backwards destroys the pre-game snapshot
`signal_report.py` grades against — silently, with the run still green, until a
grading run comes up empty weeks later. Nothing throws when this breaks, which
is why it is measured rather than reasoned about. Also pins that total failure
stays fatal and that a caller passing no `failed_sports` (`implied_total.py`)
keeps the old fail-loud contract.

Both directions were sabotage-checked when written: disabling the freeze guard
fails exactly the freeze assertion, disabling the total-failure guard fails
exactly the two fatality assertions.

No network. Succeeding builders return the real committed entities from
`data/insights.games.json`, and every store is redirected to a temp copy, so a
run cannot touch anything committed.

```
python3 -m tools.verify.test_epl_grading      # from the repo root
```

**`test_epl_grading`** — the pair of rules that decide what a DRAW does to an
EPL pick: it WINS a `double_chance` bet and LOSES a `match_result` one. Getting
those backwards throws nothing. It writes a confident wrong verdict into an
append-only ledger, on roughly one pick in four (draws are 23.6% of matches),
and makes a winning market read as a losing system. Also covers the unsettled
and malformed paths (PENDING, POSTPONED, a side naming neither club, a market
with no rule) and the adapter boundary itself — MLB's `SPORT_ADAPTERS` entries
must still be MLB's own functions, which is the property that keeps an EPL
change from reaching an MLB verdict.

Sabotage-checked in both directions when written: swapping the draw rules fails
exactly the double-chance assertions, making `match_result` push on a draw fails
exactly the match-result ones.

Offline and deterministic. `epl_matches_fixture.json` is REAL ESPN data captured
from live responses over four 2025/26 matchdays — 21 completed matches, 7 home
wins, 8 draws, 6 away wins — trimmed to the fields the adapter reads. Real
rather than hand-written because a hand-written draw is a guess about the shape
ESPN emits for one, and that shape is exactly what is being parsed.


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
