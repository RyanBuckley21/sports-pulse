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

`test_slate_dates` also covers **falling forward**: a window tuned for a
sport's usual cadence goes blank in any gap longer than itself, and then the
tab shows nothing while the fixtures it would show sit in the schedule already,
fully scoreable. NFL hit exactly that — the 2026 season opened nine days out
against a seven-day window, so week 1, whose picks come from last season's
margin and cannot change between then and kickoff, rendered as an empty tab.
The lookahead CAP is tested as hard as the fall-forward itself, because it is
the point rather than a safety rail: a real offseason has to stay visibly
empty, since "nothing on" is information. Sabotage-checked both ways — never
falling forward, and removing the cap.

It also pins the `qualifier` a Pulse carries when it was computed over a
different window than the card implies — CFB week 0, where a program's only
form is last season's. Two assertions with opposite failure modes: the text
missing entirely, and the season folded into the band WORD, which would miss
`pulseBand()`'s lookup table and silently drop every one of those cards to the
grey "cool" band.

`game_only_league_fixture.json` is REAL pipeline output — a captured 2026-08-29
cfb opening slate, four games and six team profiles, exactly as
`generate_insights` emits them. Real rather than hand-written for the same reason the EPL fixture
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


```
python3 -m tools.verify.test_cfb_signals      # from the repo root
```

**`test_cfb_signals`** — the rule that decides WHICH signals a CFB lean is
built from. Before the fallback tiers, a game with no CFBD team form scored 0
and read "No clear lean": every week-0 and week-1 game, and every game of a
season running on the ESPN fallback schedule. Two schedule-derived margin
signals now fill that gap — this season's, then last season's — and both
failure modes are silent. Letting `prior_margin` into a November lean would
drag last season into a calibrated answer that was measured without it, shifting
every mid-season score with nothing to say so; letting both margin tiers in at
once would double-count one quantity over two windows. The strongest assertion
here needs no golden value: the same PPA inputs with and without the margins
attached must produce the identical dict, even with the margins pointed the
opposite way at full strength.

Sabotage-checked in both directions when written: removing the tier gate fails
exactly the four non-perturbation assertions (a calibrated 89 becomes a 37 "No
clear lean"), and reversing the tier order fails exactly the precedence one.

No network and no fixture — every input is a number handed straight to
score_game, because the gating rule is what is under test, not anybody's feed.
Whether the resulting picks win is a backtest, and those numbers live in
config.yaml's comments and `cfb_signals._FALLBACK_TIERS`.

```
python3 -m tools.verify.test_cfb_grading      # from the repo root
```

**`test_cfb_grading`** — CFB's verdicts, and four things that fail without
throwing. A TIE must be UNRESOLVED, not MISS: overtime has settled every
regulation tie since 1996, so a level score means the feed is wrong, and MISS
would write a confident wrong verdict into an append-only ledger. An OVERTIME
final must grade — ESPN keeps `name: STATUS_FINAL` and moves the overtime into
`detail: "Final/OT"`, so a grader matching the detail string would quietly
defer every OT result as PENDING. The TEAM-NAME JOIN must go through
`_team_ref` on both sides: measured over 300 competitors across six real 2025
dates, our abbreviations and ESPN's disagree for Air Force (AF vs AFA) and
Buffalo (BUF vs BUFF) — rare enough to survive spot-checking and to fail
silently in November, and comparing the two vocabularies directly would grade
every Air Force pick UNRESOLVED, making a real record read as an empty one.
And the ADAPTER BOUNDARY: MLB's and EPL's `SPORT_ADAPTERS` entries must still be
their own functions. Also pins that `config.yaml`'s `cfb.scoreboard_url` equals
`fetchers/cfb.ESPN_CFB_SCOREBOARD` — two copies for two real consumers, and
nothing else stops the grader reading a different endpoint from the builder.

Sabotage-checked in both directions when written: grading a tie as a result
fails exactly the four tie assertions, and trusting ESPN's `abbreviation` field
fails exactly the Air Force and Buffalo ones.

Offline and deterministic. `cfb_games_fixture.json` is REAL ESPN data captured
from live responses across four 2025 dates — thirteen games including a genuine
overtime final (SMU 26-20 Miami) and both abbreviation mismatches — trimmed to
the fields the adapter reads. The POSTPONED and PENDING cases are built by
editing a real event's status block, because no postponed FBS game appeared on
any date sampled; the edit is confined to the one field those branches read.

```
python3 -m tools.verify.test_slate_dates      # from the repo root
```

**`test_slate_dates`** — the boundary between one day's games and the next, and
the two bugs that came from the pipeline holding two different answers.
`generate_insights` stamped the store with `generated_at.date()` — a UTC date —
while `signal_report` graded `yesterday` in US/Eastern. Those agree for any run
between about 04:00 and 23:59 UTC, which the workflow's 13:40/15:40 crons
comfortably were, so it sat there invisible. Then GitHub began firing them nine
to eleven hours late:

```
run 62  2026-08-27 23:02Z = 19:02 ET on the 27th -> yesterday = 08-26  graded
run 63  2026-08-28 00:34Z = 20:34 ET on the 27th -> yesterday = 08-26  again
run 64  2026-08-28 23:11Z = 19:11 ET on the 28th -> yesterday = 08-27  GONE
```

Both runs of that cycle asked about the same day, so nothing ever asked about
the 27th — and run 63 rolled the store forward to a UTC date of the 28th,
taking the 27th's pre-game snapshot with it. Two days of MLB picks went
ungraded and were written into an append-only ledger as `no_store` gaps. Every
run exited exactly as designed; nothing threw. The exact timestamps are pinned
here, along with the boundary either side of 04:00 UTC.

It also covers the two consequences. `slate_date` is a new per-row stamp saying
which BUILD a row came from, because `generated_at` could not answer that — it
is carried forward per row, so on EPL's three-day and CFB's seven-day fixture
windows a fixture first seen on the 27th reports the 27th forever. That fed
both the grader's diagnostic (which printed the genuinely baffling `covers
2026-08-27/2026-08-28 …, not 2026-08-28`) and `generate_insights`' overwrite
guard, which decides whether a late run may replace a clean pre-game snapshot
with a mid-game one. Both readers now prefer the new field and fall back for
pre-field rows. And an EMPTY SLATE — a league that simply did not play — is no
longer treated as a store/date mismatch: it used to die and write a `no_store`
row, which on the real 08-26..28 window produced three phantom gaps for CFB and
two for EPL on dates neither league had a single fixture. A non-empty slate
with no overlap is still fatal, and that contrast is asserted.

Sabotage-checked in all three directions when written: reverting the slate date
to UTC fails exactly the two source assertions, carrying `slate_date` forward
like `generated_at` fails exactly the freshness one, and removing the
empty-slate branch fails exactly the three clean-exit ones.

Offline: no network, no committed file touched, and the one grading path
exercised uses a stub slate. Both stdout and stderr are captured, because
`die()` writes to stderr and a test watching one stream would call a fatal
message "missing".

```
python3 -m tools.verify.test_nfl              # from the repo root
```

**`test_nfl`** — NFL's fallback tiers and its grading rules. Two groups, both
covering things that fail silently.

The tiers exist because nflverse publishes a season's `stats_team` release only
once that season has games, so **every week-1 game scored 0 / "No clear lean",
every year**. Two schedule-derived margin tiers fill it, and the property under
test is that a calibrated lean never contains one and a fallback lean never
contains more than one. THE BUG THAT MADE THIS FILE NECESSARY is pinned
explicitly: tier 0 is "the signals this bet type WEIGHTS", not "every declared
spec". NFL declares two specs carrying no weight — `scoring_margin` (excluded
for collinearity with off_epa) and `rest_diff` (dropped by the calibration) —
and `games.csv` publishes rest for FUTURE games. So a week-1 matchup with no
play data at all still had a non-None `rest_diff`; reading that as "tier 0 has
something" suppressed both fallbacks, while rest, carrying no weight,
contributed nothing in their place. Every opening-weekend game scored 0 — the
exact state the fallbacks exist to end. It was found by building the real 2026
opener, not by reasoning, which is why it is measured here now.

On grading, **a tie is a PUSH**. NFL overtime need not produce a winner and
about one game a season ends level; every book returns the stake. `cfb_grading`
returns UNRESOLVED for the same score line, because college football abolished
ties in 1996 and there a level score means the feed is broken — same shape,
opposite meaning, which is why the two are separate files rather than one
"football" grader. Grading a tie MISS is a quiet once-a-season wrong verdict in
an append-only ledger. And **the store's key is not the feed's key**: the store
is keyed by nflverse's `game_id` (`2026_01_DAL_PHI`), which ESPN has never heard
of, so `fetch_slate` re-keys the scoreboard through `games.csv`'s `espn` column
and drops any ESPN event with no nflverse counterpart rather than keeping it
under an id the store can never match.

Sabotage-checked in three directions when written: keying tier 0 on declared
specs rather than weights fails exactly the three unweighted-spec assertions,
grading a tie as a result fails exactly the four tie assertions, and keying the
slate by ESPN's id yields ids no store can match.

`nfl_games_fixture.json` is REAL ESPN data captured across five dates — 11
games including overtime finals and the genuine 40-40 GB-at-DAL tie of
2025-09-28, found by scanning `games.csv` for `result=0` rather than hoping one
turned up in a sample. PENDING and POSTPONED are built by editing a real
event's status block; no postponed game appeared on any date sampled.

```
python3 -m tools.verify.test_epl_coldstart    # from the repo root
```

**`test_epl_coldstart`** — what August scores on. EPL form never crosses a
season boundary (promotion and relegation turn over three clubs a summer, so
last season's table is a different league), so below `MIN_MATCHES` `score_game`
returned `{}` for every fixture: no lean, no Signal Score, **nothing to bet**,
through the whole of August and most of September, every season. EPL was the
last active sport producing no scores at all.

ONE TIER, not the two CFB and NFL carry, and the asymmetry is deliberate: those
sports need a mid-season fallback because their calibrated signals vanish for
reasons unrelated to the calendar (no CFBD budget, an unjoinable schedule
source, an unpublished nflverse release). EPL's inputs come from the same
scoreboard as its fixtures, so above the gate the weighted model is always
there — and `MIN_MATCHES = 5` already puts the handoff at match 6, which is
where the measurement puts it.

Four things are pinned, all silent when wrong. **The `{}` contract survives** —
a cold match whose fallback is also empty (a promoted club, no prior Premier
League season) must still return `{}`, or the store fills with markets reading
"No clear lean" at score 0. **Above the gate nothing changes**, even with a
prior-season number pointing hard the other way. **Both markets, one lean** —
double_chance and match_result score off the same lean at bars 55 and 75, and
the fallback feeds both. And **the card cannot lie**: unlike CFB and NFL, an EPL
club under the gate usually HAS played, so the card would print "3.00 goals per
match" off a single 3-0 beside a lean that deliberately ignored it. The
prior-season row leads and the in-season rows carry their denominator.

Sabotage-checked in three directions: not dropping the fallback above the gate
fails exactly the four no-change assertions, removing the `{}` guard fails
exactly the three promoted-club ones, and dropping the half-season floor fails
exactly the exclusion ones.

No network — every input is a number handed straight to `score_game`.

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
