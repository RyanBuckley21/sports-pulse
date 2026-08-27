/*
 * Browser verification suite for the navigation shell.
 *
 *     python3 -m tools.verify.make_fixture   # once, writes web/data.json
 *     node tools/verify/run.js               # from the repo root
 *
 * Exits non-zero on any failure. Groups:
 *
 *   insights-scope-leak-check  The regression guard for the cascade leak found
 *                              while scoping insights.css: four class rules it
 *                              shares with app.css (.breakdown-label and the
 *                              three .vs-starter-* rules) would silently win in
 *                              the Who's Hot player detail once both sheets
 *                              live in one document. Nothing errors when this
 *                              breaks -- the detail view just quietly looks
 *                              wrong -- so it is measured, not eyeballed.
 *   re-entry                   app.js / insights.js must be mountable many
 *                              times without leaking, double-binding, or
 *                              carrying state across a section change.
 *   router                     Hash routing, cold-launch normalisation, tab
 *                              state, and the single-document guarantee.
 *   standalone                 The home-screen/PWA declarations whose absence
 *                              was the original bug.
 *
 * Groups run INDEPENDENTLY (see GROUPS near the bottom). A group that throws --
 * a page.click timing out on a selector the app no longer renders is the real
 * case -- is reported as CRASH, counted as a failure, and the remaining groups
 * still run. Before that, one such throw killed the process: the ai-note group
 * timing out meant router, safe-area and standalone never executed at all, and
 * the suite reported a Node stack trace instead of results. That silence is
 * unaffordable for a check meant to gate PRs, where the groups that vanish are
 * invisible precisely when something is already broken.
 *
 * A crashed group is PARTIALLY REPORTED -- whatever it never reached is neither
 * passed nor failed -- so crashes are surfaced separately from assertion
 * failures rather than folded into the same count. Exit code is unchanged: 0
 * only when everything ran and passed, 1 for any failure or crash, 2 when the
 * harness itself cannot start (no Playwright, no data.json), since there is
 * nothing to isolate if nothing can run.
 *
 * VERIFY_TIMEOUT_MS overrides the per-action timeout (default 30000, matching
 * Playwright's own). It bounds what a hung group costs before the runner moves
 * on, so CI can trade a slower runner's headroom for a faster red.
 *
 * Playwright is resolved from node_modules if present, otherwise from the
 * global install. This repo intentionally has no package.json; wiring the suite
 * into CI is a separate decision from having it runnable.
 */

"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
// Defaults to the source tree. Point it at a built site/ directory to verify
// the actual deploy artefact instead -- cache-busted URLs, assets in their
// shipped position -- which is the only way to catch breakage that exists only
// after the workflow assembles things:
//     VERIFY_ROOT=/path/to/site node tools/verify/run.js
const WEB = process.env.VERIFY_ROOT
  ? path.resolve(process.env.VERIFY_ROOT)
  : path.join(REPO, "web");
const IS_SOURCE_TREE = WEB === path.join(REPO, "web");

let chromium;
for (const candidate of ["playwright", "/opt/node22/lib/node_modules/playwright"]) {
  try { chromium = require(candidate).chromium; break; } catch (e) { /* try next */ }
}
if (!chromium) {
  console.error("playwright not found (tried ./node_modules and the global install)");
  process.exit(2);
}

// ---------------------------------------------------------------- tiny server
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml" };

function serve(root) {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split("?")[0].split("#")[0]);
    // The deploy copies web/* and assets/* side by side into the site root, so
    // /assets/... resolves there but not under a bare web/ root. Mapping it back
    // to the repo root reproduces the deployed layout rather than tolerating
    // 404s that would be real in production. A built site/ already has assets
    // in place, so it needs no mapping.
    const base = (IS_SOURCE_TREE && rel.startsWith("/assets/")) ? REPO : root;
    const file = path.join(base, rel === "/" ? "/index.html" : rel);
    if (!file.startsWith(base)) { res.writeHead(403).end(); return; }
    fs.readFile(file, (err, buf) => {
      if (err) { res.writeHead(404).end("not found"); return; }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
      res.end(buf);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () =>
    resolve({ server, base: "http://127.0.0.1:" + server.address().port })));
}

// ------------------------------------------------------------------- harness
let pass = 0, fail = 0, group = "";
const failures = [];
// Groups that threw rather than merely failing. Tracked apart from `failures`
// because the two say different things about how much of the suite actually ran.
const crashed = [];

function heading(name) { group = name; console.log("\n" + name); }
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("  PASS  " + name + (detail ? "  (" + detail + ")" : "")); }
  else { fail++; failures.push(group + " / " + name + (detail ? ": " + detail : ""));
         console.log("  FAIL  " + name + (detail ? "  (" + detail + ")" : "")); }
}

// How long any single Playwright action may block. Stated explicitly rather
// than left to Playwright's implicit default, because with per-group isolation
// this is the ONLY thing bounding a crashed group -- every way this suite can
// hang goes through an action, so it is what a hung group costs before the
// runner moves on to the next one. The value IS Playwright's 30s default, so
// timing is unchanged; it lives here so CI can dial it down
// (VERIFY_TIMEOUT_MS=10000) without editing the suite.
const ACTION_TIMEOUT_MS = Number(process.env.VERIFY_TIMEOUT_MS) || 30000;

// Fonts are remote; blocking them keeps the suite offline-deterministic and
// stops networkidle waits hanging on a host that cannot reach Google.
async function newPage(browser, opts) {
  const p = await browser.newPage(Object.assign({ viewport: { width: 430, height: 900 } }, opts));
  p.setDefaultTimeout(ACTION_TIMEOUT_MS);
  await p.route("**://fonts.*/**", (r) => r.abort());
  return p;
}

function collectProblems(page, base) {
  const problems = [];
  page.on("pageerror", (e) => problems.push("threw: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !/fonts\.|ERR_FAILED|ERR_CONNECTION/.test(m.text())) {
      problems.push("console: " + m.text());
    }
  });
  page.on("response", (r) => {
    if (r.status() >= 400 && !/fonts\./.test(r.url())) {
      problems.push("HTTP " + r.status() + " " + r.url().replace(base, ""));
    }
  });
  return problems;
}

const ROUTES = ["#/", "#/games", "#/players", "#/teams"];

// Navigate the way a user would, through the router, so container visibility and
// the scope class are applied. Calling a view's mount() directly exercises the
// module contract but leaves the shell untouched.
async function goRoute(page, hash) {
  await page.evaluate((h) => { window.location.hash = h; }, hash);
  await page.waitForTimeout(450);
}

// ------------------------------------------------------- insights-scope-leak
// Loads app.css and insights.css together -- what the shell does -- and reads
// the computed values the Who's Hot player detail depends on, with the scope
// class off and on. Off must equal app.css; on must equal insights.css.
async function scopeLeakCheck(browser, base) {
  const p = await newPage(browser);
  // "load", not "domcontentloaded": computed styles are only meaningful once
  // the stylesheets have applied.
  await p.goto(base + "/__leak.html", { waitUntil: "load" });
  await p.waitForTimeout(200);

  // THE DISCRIMINATING PROPERTIES CHANGED IN PHASE 3, AND THE CHECK HAD TO
  // FOLLOW. This used to read fontFamily on .breakdown-label / .vs-starter-line
  // -- "JetBrains Mono" unscoped vs "Space Grotesk" scoped -- which was the
  // sharpest available signal while app.css was a mono-typeset stylesheet. Both
  // files now resolve to var(--font-body), so fontFamily is IDENTICAL in both
  // states: keeping it would have left an assertion that can no longer fail,
  // which is worse than no assertion because it still reads like coverage.
  //
  // Swapped for the properties where the two rules genuinely still disagree --
  // weight (500 vs 600) and letter-spacing (0.3px vs -0.01em) -- so the check
  // keeps measuring what it was built to measure: that insights.css, which
  // loads second, does not win on selectors app.css also owns.
  const read = () => p.evaluate(() => {
    const g = (sel, prop) => getComputedStyle(document.querySelector(sel))[prop];
    return {
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      breakdownLabelWeight: g(".breakdown-label", "fontWeight"),
      breakdownLabelMargin: g(".breakdown-label", "marginBottom"),
      vsStarterLineSpacing: g(".vs-starter-line", "letterSpacing"),
      vsStarterSectionMargin: g(".vs-starter-section", "marginTop"),
      vsStarterCaveatSpacing: g(".vs-starter-caveat", "letterSpacing"),
    };
  });

  const APP = {  // app.css -- what Who's Hot must keep
    breakdownLabelWeight: "500",
    breakdownLabelMargin: "4px",
    vsStarterLineSpacing: "0.3px",
    vsStarterSectionMargin: "22px",
    vsStarterCaveatSpacing: "0.5px",
  };
  const INSIGHTS = {  // insights.css -- what the insights section must get
    breakdownLabelWeight: "600",
    breakdownLabelMargin: "10px",
    vsStarterLineSpacing: "-0.15px",
    vsStarterSectionMargin: "18px",
    vsStarterCaveatSpacing: "0.4px",
  };

  const off = await read();
  for (const k of Object.keys(APP)) {
    ok("unscoped keeps app.css " + k, off[k] === APP[k], off[k] + " vs " + APP[k]);
  }
  await p.evaluate(() => document.body.classList.add("insights-scope"));
  const on = await read();
  for (const k of Object.keys(INSIGHTS)) {
    ok("scoped applies insights.css " + k, on[k] === INSIGHTS[k], on[k] + " vs " + INSIGHTS[k]);
  }

  // The inverse of everything above, and new in Phase 3: the page background
  // must now be THE SAME in both states. It was the clearest evidence of the
  // two-palette document (rgb(10,10,11) unscoped, rgb(12,13,16) scoped) --
  // toggling the class re-themed the canvas. One consolidated :root is what
  // lets a glass surface read as the same material on Who's Hot and on
  // Players, so a difference here now means someone reintroduced a
  // section-scoped token block.
  ok("one palette: the canvas does not re-theme on scope",
     off.bodyBackground === on.bodyBackground,
     "unscoped=" + off.bodyBackground + " scoped=" + on.bodyBackground);
  ok("  and it is the consolidated --bg", on.bodyBackground === "rgb(12, 13, 16)",
     on.bodyBackground);
  await p.close();
}

// ------------------------------------------------------------------ re-entry
async function reEntryChecks(browser, base) {
  const p = await newPage(browser);
  let json = [];
  p.on("request", (r) => { if (/\.json/.test(r.url())) json.push(r.url().replace(base, "")); });
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(700);

  ok("leaderboard renders on load", (await p.$$eval(".player-row", (e) => e.length)) > 0);
  ok("one data fetch on load", json.length === 1, json.join(","));

  // Detail -> leave section -> return must land on the list, as a reload does.
  // Detects the detail view by .pcat-list, not the old .hero-value: the 68px
  // hero was retired when the page became a multi-category list, and .pcat-list
  // is now the element that exists there and nowhere else.
  await p.click(".player-row");
  await p.waitForTimeout(250);
  const inDetail = await p.evaluate(() => !!document.querySelector(".pcat-list"));
  ok("player detail opens", inDetail);
  json = [];
  await p.evaluate(() => { SP.views.whosHot.unmount(); return SP.views.whosHot.mount(); });
  await p.waitForTimeout(300);
  ok("unmount+mount returns to the list",
     (await p.$$eval(".player-row", (e) => e.length)) > 0 &&
     !(await p.evaluate(() => !!document.querySelector(".pcat-list"))));
  ok("re-mount inside the staleness window does not refetch", json.length === 0, json.join(",") || "none");

  // Scroll reset: [hidden] clears descendant scroll but never the document's.
  await p.evaluate(() => document.documentElement.style.minHeight = "3000px");
  await p.evaluate(() => window.scrollTo(0, 500));
  await p.waitForTimeout(120);
  const before = await p.evaluate(() => window.scrollY);
  await p.evaluate(() => SP.views.whosHot.mount());
  await p.waitForTimeout(250);
  const after = await p.evaluate(() => window.scrollY);
  ok("mount resets document scroll", before > 0 && after === 0, before + " -> " + after);
  await p.evaluate(() => document.documentElement.style.minHeight = "");

  // Staleness threshold fires only past the window.
  json = [];
  await p.evaluate(() => SP.views.whosHot.mount());
  await p.waitForTimeout(250);
  ok("fresh copy: still no refetch", json.length === 0, json.join(",") || "none");
  await p.evaluate(() => { const real = Date.now; Date.now = () => real() + 11 * 60 * 1000; });
  json = [];
  await p.evaluate(() => SP.views.whosHot.mount());
  await p.waitForTimeout(400);
  ok("past the staleness window: refetches", json.length === 1, json.join(",") || "none");
  await p.evaluate(() => { delete Date.now; });

  // Insights cache is keyed by source file, not by view.
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(700);
  json = [];
  await p.evaluate(() => SP.views.insights.mount("games"));
  await p.waitForTimeout(400);
  ok("first insights view: one fetch", json.length === 1, json.join(",") || "none");
  json = [];
  await p.evaluate(() => SP.views.insights.mount("players"));
  await p.waitForTimeout(400);
  ok("view sharing the same source: no fetch", json.length === 0, json.join(",") || "none");
  json = [];
  await p.evaluate(() => SP.views.insights.mount("teams"));
  await p.waitForTimeout(400);
  ok("teams now shares data.json too: no fetch", json.length === 0, json.join(",") || "none");
  // Components is the last view still on the mock, so it is what proves the
  // cache keys on SOURCE rather than view. This assertion used to be teams'
  // job; teams moved to the live pipeline, components did not.
  json = [];
  await p.evaluate(() => SP.views.insights.mount("components"));
  await p.waitForTimeout(400);
  ok("view on the other source: exactly one fetch", json.length === 1, json.join(",") || "none");
  ok("and it is the mock", /mock-insights\.json/.test(json[0] || ""), json[0] || "none");

  // The accordion must start collapsed on every visit (insights.js:566) --
  // which only holds because mount() re-renders instead of re-showing cached DOM.
  // Routed rather than mounted directly: the container is only made visible by
  // the router, and these checks need to actually click.
  await goRoute(p, "#/games");
  await p.click(".gr-row");
  await p.waitForTimeout(200);
  ok("game row expands", (await p.$$eval(".gr-item.is-open", (e) => e.length)) === 1);
  await goRoute(p, "#/teams");
  await goRoute(p, "#/games");
  ok("leaving and returning collapses it again", (await p.$$eval(".gr-item.is-open", (e) => e.length)) === 0);

  // Delegated handlers are bound once at load to permanent nodes: repeated
  // mounts must neither lose them nor stack them.
  for (let i = 0; i < 5; i++) {
    await p.evaluate(() => SP.views.insights.mount("games"));
    await p.waitForTimeout(120);
  }
  await goRoute(p, "#/games");
  await p.click(".gr-row");
  await p.waitForTimeout(200);
  ok("clicks still work after five re-mounts",
     (await p.$$eval(".gr-item.is-open", (e) => e.length)) === 1);
  await p.close();
}

// ------------------------------------------------------- player-detail
// The detail page lists EVERY board a player qualifies on, hottest first, each
// collapsing into its per-category breakdown. Three things here are only ever
// wrong silently -- the page still renders, it just shows the wrong boards, in
// the wrong order, or quietly stops deferring them -- so all three are measured.
//
// Ordering is the subtlest. The fixture deliberately gives its multi-board
// player DIFFERENT ranks per board (1/2/3/4/6), because a fixture where
// everyone is #1 everywhere lets a broken sort pass.
async function playerDetailChecks(browser, base) {
  const p = await newPage(browser);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);

  const read = () => p.evaluate(() => {
    const items = [...document.querySelectorAll(".pcat-item")];
    return {
      count: items.length,
      labels: items.map((i) => i.querySelector(".pcat-label").textContent),
      ranks: items.map((i) => Number(i.querySelector(".pcat-rank").textContent.replace("#", ""))),
      // Bar width is the pulse score, so it must fall as rank rises. Read the
      // inline width rather than the rendered px, which varies with viewport.
      widths: items.map((i) => parseFloat(i.querySelector(".mag-fill").style.width)),
      open: items.map((i) => i.classList.contains("is-open")),
      aria: items.map((i) => i.querySelector("[data-pcat-btn]").getAttribute("aria-expanded")),
      // Measured, not inferred from the class: a collapsed section that still
      // has height is the failure mode a class check cannot see.
      heights: items.map((i) => Math.round(i.querySelector(".pcat-detail-inner").getBoundingClientRect().height)),
      heatDots: items.map((i) => !!i.querySelector(".heat-dot")),
    };
  });

  // Aaron Judge -- five boards at ranks 1/2/3/4/6. He is #2 on home_runs, the
  // first board, so tapping row 2 of the default leaderboard reaches him.
  await p.evaluate(() => document.querySelectorAll(".player-row")[1].click());
  await p.waitForTimeout(300);
  let s = await read();

  ok("multi-board player lists every board", s.count === 5, s.count + " -> " + s.labels.join(", "));
  ok("  ranks ascending (hottest first)",
     s.ranks.every((r, i) => i === 0 || s.ranks[i - 1] <= r), s.ranks.join(","));
  ok("  bar length falls with rank",
     s.widths.every((w, i) => i === 0 || s.widths[i - 1] >= w), s.widths.join(","));
  // 100 - (rank-1)*7, the _pulse() formula ported into app.js.
  ok("  bar encodes pulse, not share-of-leader",
     s.widths.every((w, i) => w === Math.max(30, Math.min(100, 100 - (s.ranks[i] - 1) * 7))),
     s.ranks.map((r, i) => "#" + r + "=" + s.widths[i] + "%").join(" "));
  ok("  exactly one heat dot, on the hottest board",
     s.heatDots.filter(Boolean).length === 1 && s.heatDots[0], s.heatDots.join(","));

  // The premise of the redesign: no category is privileged, INCLUDING the one
  // tapped in from. Judge was reached via home_runs, which is not first here.
  ok("  nothing expanded on arrival", s.open.every((o) => o === false), s.open.join(","));
  ok("  and collapsed means zero height", s.heights.every((h) => h === 0), s.heights.join(","));
  ok("  aria-expanded matches", s.aria.every((a) => a === "false"), s.aria.join(","));
  ok("  the tapped category is not promoted to the top",
     s.labels[0] === "Total Bases / G", s.labels[0]);

  // The retired hero: its absence is the change, so assert it rather than
  // trusting that nothing re-adds a 68px restatement of the tapped row.
  ok("  no hero restating the leaderboard row",
     (await p.$$eval(".hero-value, .hero-row", (e) => e.length)) === 0);

  await p.evaluate(() => document.querySelectorAll("[data-pcat-btn]")[2].click());
  await p.waitForTimeout(450);
  s = await read();
  ok("tapping a row expands it in place", s.open[2] === true && s.aria[2] === "true",
     "open=" + s.open.join(",") + " aria=" + s.aria.join(","));
  ok("  the section has height", s.heights[2] > 0, s.heights[2] + "px");
  ok("  and the others stay closed (accordion)",
     s.open.filter(Boolean).length === 1, s.open.join(","));
  ok("  the full per-category detail is inside it",
     await p.evaluate(() => {
       const d = document.querySelectorAll(".pcat-item")[2];
       return !!(d.querySelector(".key-row") && d.querySelector(".bars-row") && d.querySelector(".breakdown-row"));
     }));

  await p.evaluate(() => document.querySelectorAll("[data-pcat-btn]")[0].click());
  await p.waitForTimeout(450);
  s = await read();
  ok("opening another closes the first", s.open[0] === true && s.open[2] === false,
     s.open.join(","));

  await p.evaluate(() => document.querySelectorAll("[data-pcat-btn]")[0].click());
  await p.waitForTimeout(450);
  s = await read();
  ok("tapping the open row closes it", s.open.every((o) => o === false) && s.heights[0] === 0,
     "open=" + s.open.join(",") + " h=" + s.heights[0]);

  // vs-next-starter used to render once PER CATEGORY -- identical every time,
  // since fetchers/mlb.py caches it per team and per (batter, pitcher) pair,
  // never per stat category. Judge is on 5 hitting boards here, so 5 identical
  // copies would be the old bug; exactly 1 is the fix.
  const vs = await p.evaluate(() => {
    const sections = [...document.querySelectorAll(".vs-starter-section")];
    const list = document.querySelector(".pcat-list");
    return {
      count: sections.length,
      insideAnyRow: sections.some((el) => el.closest(".pcat-item") !== null),
      // Sibling of .pcat-list (page level), not a descendant of it.
      isPageLevel: sections.length === 1 && sections[0].parentElement === list.parentElement,
      // Comes after the category list in document order, per the spec.
      afterList: sections.length === 1 &&
        !!(list.compareDocumentPosition(sections[0]) & Node.DOCUMENT_POSITION_FOLLOWING),
      text: sections[0] && sections[0].textContent,
    };
  });
  ok("vs-next-starter renders exactly once, not once per category",
     vs.count === 1, "count=" + vs.count);
  ok("  at the page level, not inside any category row",
     !vs.insideAnyRow, "insideAnyRow=" + vs.insideAnyRow);
  ok("  as a sibling of .pcat-list, after it", vs.isPageLevel && vs.afterList,
     "isPageLevel=" + vs.isPageLevel + " afterList=" + vs.afterList);
  ok("  showing the shared matchup", /Chris Sale/.test(vs.text || ""), JSON.stringify(vs.text));

  // Back must still work, and still land where the reader left the list.
  await p.evaluate(() => { document.documentElement.style.minHeight = "3000px"; window.scrollTo(0, 300); });
  await p.waitForTimeout(150);
  await p.evaluate(() => document.querySelector("#backBtn").click());
  await p.waitForTimeout(350);
  ok("back returns to the leaderboard",
     (await p.$$eval(".player-row", (e) => e.length)) > 0 &&
     (await p.$$eval(".pcat-item", (e) => e.length)) === 0);

  // N=1: a player on a single board. A collapsed row would put a tap between
  // the reader and the only thing the page has to say, so it opens expanded.
  await p.evaluate(() => {
    document.querySelector('[data-cat="strikeouts"]').click();
  });
  await p.waitForTimeout(350);
  await p.evaluate(() => document.querySelector(".player-row").click());
  await p.waitForTimeout(350);
  s = await read();
  ok("single-board player auto-expands", s.count === 1 && s.open[0] === true,
     "count=" + s.count + " open=" + s.open.join(","));
  ok("  with aria and height agreeing", s.aria[0] === "true" && s.heights[0] > 0,
     "aria=" + s.aria[0] + " h=" + s.heights[0]);

  await p.close();
}

// ------------------------------------------------------------ detail-history
// Entering the player-detail page used to be a pure state swap with no
// history footprint at all -- confirmed live: native OS edge-swipe-back had
// no entry of ours to pop, so it fell through to whatever hash-route entry
// was on the stack before the CURRENT Who's Hot visit (i.e. whichever tab was
// open immediately before it), landing on THAT tab instead of returning here.
//
// Playwright has no literal iOS edge-swipe simulator, so page.goBack() is
// used as the closest available equivalent: it drives the same underlying
// primitive a native gesture does (moves the browser's real session-history
// position back by one, firing a genuine popstate) rather than anything the
// app's own click/touch handlers are involved in. That is precisely the path
// the fix's popstate listener exists for, and precisely the path the original
// bug happened on.
async function detailHistoryChecks(browser, base) {
  const p = await newPage(browser);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);

  // ---- reproduce the ORIGINAL bug's exact setup ----
  // Visit Players, then come back to Who's Hot, THEN open a detail view. Before
  // the fix this is exactly the sequence that made native back land on Players.
  await goRoute(p, "#/players");
  await goRoute(p, "#/");
  await p.waitForTimeout(200);

  await p.evaluate(() => window.scrollTo(0, 300));
  await p.waitForTimeout(100);
  const scrollBeforeOpen = await p.evaluate(() => window.scrollY);
  await p.evaluate(() => document.querySelectorAll(".player-row")[1].click());
  await p.waitForTimeout(300);

  const opened = await p.evaluate(() => ({
    hash: location.hash,
    state: history.state,
    onDetail: !!document.querySelector(".pcat-list"),
  }));
  ok("opening detail pushes a history entry", opened.state && opened.state.whosHotDetail === true,
     JSON.stringify(opened.state));
  ok("  without changing the hash (router must not see this)", opened.hash === "#/", opened.hash);

  // ---- the actual regression test: real back-navigation, not the in-app button ----
  await p.goBack();
  await p.waitForTimeout(400);
  const afterNativeBack = await p.evaluate(() => ({
    hash: location.hash,
    appHidden: document.getElementById("app").hidden,
    insightsHidden: document.getElementById("insightsView").hidden,
    onList: !!document.querySelector(".board"),
    scroll: window.scrollY,
    activeTab: document.querySelector('.tab[aria-current="page"]').getAttribute("data-route"),
  }));
  ok("native back-navigation returns to the Who's Hot LIST, not the previous tab",
     afterNativeBack.onList && !afterNativeBack.appHidden && afterNativeBack.insightsHidden,
     JSON.stringify(afterNativeBack));
  ok("  the active tab is still Who's Hot", afterNativeBack.activeTab === "whos-hot",
     afterNativeBack.activeTab);
  ok("  the hash never changed (router took no part in this)",
     afterNativeBack.hash === "#/", afterNativeBack.hash);
  ok("  scroll position survives the round trip",
     afterNativeBack.scroll === scrollBeforeOpen,
     "before=" + scrollBeforeOpen + " after=" + afterNativeBack.scroll);

  // ---- the in-app back button: same destination, and the stack stays flat ----
  // history.length never DECREASES on back() (it only repositions; going back
  // leaves a now-stale forward entry sitting past the current position), so
  // flatness is only observable by comparing length AFTER each round trip
  // completes: the next push must truncate that stale forward entry rather
  // than stacking a new one behind it. Two full round trips, different rows.
  const lens = [];
  for (let i = 0; i < 2; i++) {
    await p.evaluate((idx) => document.querySelectorAll(".player-row")[idx].click(), i);
    await p.waitForTimeout(250);
    await p.evaluate(() => document.querySelector("#backBtn").click());
    await p.waitForTimeout(250);
    lens.push(await p.evaluate(() => history.length));
  }
  ok("in-app back button also returns to the list",
     await p.evaluate(() => !!document.querySelector(".board")));
  ok("  repeated visits to different rows do not deepen the history stack",
     lens[0] === lens[1], "lengths=" + lens.join(","));

  // ---- scroll survives the in-app back button too, not just native back ----
  await p.evaluate(() => window.scrollTo(0, 250));
  await p.waitForTimeout(100);
  const scrollBeforeBtn = await p.evaluate(() => window.scrollY);
  await p.evaluate(() => document.querySelectorAll(".player-row")[2].click());
  await p.waitForTimeout(300);
  await p.evaluate(() => document.querySelector("#backBtn").click());
  await p.waitForTimeout(300);
  const scrollAfterBtn = await p.evaluate(() => window.scrollY);
  ok("scroll position survives the in-app back button",
     scrollAfterBtn === scrollBeforeBtn,
     "before=" + scrollBeforeBtn + " after=" + scrollAfterBtn);

  // ---- the hand-rolled swipe-to-dismiss gesture, kept deliberately untouched
  // by this fix, still has to work: real Touch objects, not plain shapes, or
  // Chromium rejects the TouchEvent construction outright.
  await p.evaluate(() => window.scrollTo(0, 180));
  await p.waitForTimeout(100);
  const scrollBeforeSwipe = await p.evaluate(() => window.scrollY);
  await p.evaluate(() => document.querySelectorAll(".player-row")[0].click());
  await p.waitForTimeout(300);
  const lenBeforeSwipe = await p.evaluate(() => history.length);
  await p.evaluate(() => {
    const target = document.querySelector("#app .wrap");
    function fire(type, x, y) {
      const t = new Touch({ identifier: 0, target, clientX: x, clientY: y });
      const ev = new TouchEvent(type, {
        touches: type === "touchend" ? [] : [t], changedTouches: [t],
        bubbles: true, cancelable: true,
      });
      document.getElementById("app").dispatchEvent(ev);
    }
    fire("touchstart", 20, 400);
    fire("touchmove", 60, 400);
    fire("touchmove", 160, 400); // clearly rightward, past the 12px decide threshold
    fire("touchend", 160, 400);
  });
  await p.waitForTimeout(400); // 180ms slide transition + goBack()'s 170ms setTimeout
  const afterSwipe = await p.evaluate(() => ({
    onList: !!document.querySelector(".board"), scroll: window.scrollY, len: history.length,
  }));
  ok("the hand-rolled swipe gesture still returns to the list, untouched by this fix",
     afterSwipe.onList, JSON.stringify(afterSwipe));
  ok("  and still restores scroll", afterSwipe.scroll === scrollBeforeSwipe,
     "before=" + scrollBeforeSwipe + " after=" + afterSwipe.scroll);
  ok("  and still pairs its own push/pop (flat stack)", afterSwipe.len === lenBeforeSwipe,
     "before=" + lenBeforeSwipe + " after=" + afterSwipe.len);

  await p.close();
}

// ------------------------------------------------------------- ai-note
// The AI note is a SECOND, NESTED disclosure inside the row-level accordion,
// and the two are independent: a note lives inside an open row, so opening a
// row must not reveal its note and revealing a note must not disturb the row.
// That independence is the thing most likely to break silently -- nothing
// throws, the card just starts giving away the paragraph it was meant to defer
// (or the row starts collapsing when you tap the badge).
//
// Also covers the shared-component claim. .ai-summary is emitted by
// gameInsight, playerInsight, teamInsight and the gallery from ONE function, so
// forking the behaviour per screen is the regression to watch for: the checks
// run the same assertions on Games and on Players.
//
// GATED ON data.aiInsightsEnabled. config.yaml has ai_insights.enabled: false,
// so real pipeline output carries the flag false and Cards.aiSummary returns ""
// for every entity -- there is no note to disclose and every assertion below is
// unanswerable. That is not a regression, it is the flag doing its job, so the
// disclosure checks are skipped. What replaces them is the flag's OWN contract,
// which is just as testable and currently untested: nothing renders a note
// anywhere. Skipping outright would leave that unverified, and a leak past a
// disabled flag is the more expensive bug of the two -- it ships AI prose from
// a build that was configured to have none.
//
// The fixture omits the key entirely, which is the third case on purpose:
// insights.js defaults AI_ENABLED true on absence (mock-insights.json predates
// the flag), so `node tools/verify/run.js` after make_fixture still runs the
// full disclosure suite. Point VERIFY_ROOT at a built site/ to exercise the
// disabled path against what actually deploys.
async function aiNoteChecks(browser, base) {
  const p = await newPage(browser);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);

  // Read from the same data.json the app just fetched, not from config.yaml:
  // this asserts against the artefact under test, which is the only thing that
  // can be wrong here. A built site/ carries its own data.json.
  const aiEnabled = JSON.parse(
    fs.readFileSync(path.join(WEB, "data.json"), "utf8")).aiInsightsEnabled !== false;
  if (!aiEnabled) {
    console.log("  SKIP  disclosure checks -- data.json has aiInsightsEnabled: false");
    for (const route of ["#/games", "#/players"]) {
      await goRoute(p, route);
      await p.waitForTimeout(300);
      // Open a row: the note is built lazily inside it, so an unopened row
      // would report zero notes whether the flag worked or not.
      const row = route === "#/games" ? ".gr-row" : ".pi-row";
      await p.click(row);
      await p.waitForTimeout(400);
      const found = await p.evaluate(() => ({
        boxes: document.querySelectorAll(".ai-summary").length,
        heads: document.querySelectorAll("[data-ainote]").length,
      }));
      ok("the flag is off, so " + route + " renders no AI note",
         found.boxes === 0 && found.heads === 0,
         "ai-summary=" + found.boxes + " data-ainote=" + found.heads);
      // The gate is on the note alone -- the deterministic card must survive it.
      const detail = await p.$$eval(
        route === "#/games" ? ".gr-item.is-open .gr-detail" : ".pi-item.is-open .pi-detail",
        (e) => e.length);
      ok("  and the row still opens its deterministic detail", detail === 1,
         "open details=" + detail);
    }
    await p.close();
    return;
  }

  const state = () => p.evaluate(() => {
    const box = document.querySelector(".ai-summary");
    if (!box) return null;
    const btn = box.querySelector("[data-ainote]");
    const body = box.querySelector(".ai-body");
    return {
      revealed: box.classList.contains("is-revealed"),
      aria: btn && btn.getAttribute("aria-expanded"),
      isButton: btn && btn.tagName,
      rows: body && getComputedStyle(body).gridTemplateRows,
      // The measured height of the clipped inner is what the user actually
      // gets; the class could be right while the CSS silently failed.
      innerH: Math.round(box.querySelector(".ai-body-inner").getBoundingClientRect().height),
      openRows: document.querySelectorAll(".gr-item.is-open").length,
    };
  });

  // ---- Games ----
  await goRoute(p, "#/games");
  await p.click(".gr-row");
  await p.waitForTimeout(400);
  let s = await state();
  ok("games: an expanded row renders an AI note", s !== null);
  ok("  it is collapsed on open", s.revealed === false && s.aria === "false",
     "revealed=" + s.revealed + " aria-expanded=" + s.aria);
  ok("  and collapsed means zero height, not just an unset class", s.innerH === 0, s.innerH + "px");
  // Row-level and note-level state must not have been conflated.
  ok("  opening the row did not reveal the note", s.openRows === 1 && !s.revealed,
     "openRows=" + s.openRows + " revealed=" + s.revealed);
  // A real button, so the platform supplies Enter/Space and focus.
  ok("  the control is a real button", s.isButton === "BUTTON", String(s.isButton));
  // Through getByRole, not textContent: the chevron is aria-hidden, so it is in
  // the text but must NOT be in the accessible name. textContent cannot tell the
  // difference and would pass on a button that announces "AI Note >" to a screen
  // reader. This runs the real accessible-name computation.
  const named = await p.getByRole("button", { name: "AI Note", exact: true }).count();
  ok("  named by the badge, with the chevron excluded", named >= 1, "matches=" + named);

  await p.click("[data-ainote]");
  await p.waitForTimeout(450);
  s = await state();
  ok("games: tapping the header reveals it", s.revealed === true && s.aria === "true",
     "revealed=" + s.revealed + " aria-expanded=" + s.aria);
  ok("  the body actually has height", s.innerH > 0, s.innerH + "px");
  ok("  it uses the grid-rows mechanism, not display", s.rows !== "0px", s.rows);
  ok("  revealing the note left the row open", s.openRows === 1, "openRows=" + s.openRows);

  await p.click("[data-ainote]");
  await p.waitForTimeout(450);
  s = await state();
  ok("games: tapping again collapses it", s.revealed === false && s.innerH === 0,
     "revealed=" + s.revealed + " innerH=" + s.innerH);

  // The clamp/"Read full note" pair this disclosure replaced. Two gates on one
  // paragraph was the thing being removed, so its return is a regression.
  const stale = await p.evaluate(() => ({
    readmore: document.querySelectorAll("[data-readmore], .ai-readmore").length,
    clamped: document.querySelectorAll(".ai-summary.clamp").length,
  }));
  ok("no second gate behind the first", stale.readmore === 0 && stale.clamped === 0,
     "readmore=" + stale.readmore + " clamp=" + stale.clamped);

  // ---- Players: same component, so the same behaviour, unforked ----
  // The players list is an accordion now ("players: collapsible rows, accordion
  // like Games"), so playerInsight -- and the AI note inside it -- is built
  // lazily into .pi-detail-inner on first tap, exactly like gameInsight. These
  // checks used to read the note straight off #/players because the card was
  // rendered inline there; that is no longer a thing the page does, so the row
  // gets opened first, the same way the Games half above does.
  const noteCount = () => p.$$eval(".ai-summary", (e) => e.length);
  await goRoute(p, "#/players");
  await p.waitForTimeout(300);
  ok("players: the list arrives with no note built yet", (await noteCount()) === 0,
     "notes=" + (await noteCount()));
  await p.click(".pi-row");
  await p.waitForTimeout(400);
  s = await state();
  ok("  an expanded row renders one", s !== null);
  ok("  collapsed on open, same as games", s.revealed === false && s.innerH === 0,
     "revealed=" + s.revealed + " innerH=" + s.innerH);
  await p.click("[data-ainote]");
  await p.waitForTimeout(450);
  s = await state();
  ok("  and reveals the same way", s.revealed === true && s.innerH > 0,
     "revealed=" + s.revealed + " innerH=" + s.innerH);

  // Same guarantee the row accordion has: state is a class on re-rendered DOM,
  // so leaving and returning must reset it rather than restore it. With the
  // accordion that now holds at BOTH levels -- the row closes, and the note
  // behind it comes back collapsed rather than remembering it was open.
  await goRoute(p, "#/games");
  await goRoute(p, "#/players");
  await p.waitForTimeout(300);
  ok("  leaving and returning closes the row that held it", (await noteCount()) === 0,
     "notes=" + (await noteCount()));
  await p.click(".pi-row");
  await p.waitForTimeout(400);
  s = await state();
  ok("  and its note is rebuilt collapsed, not restored open",
     s !== null && s.revealed === false && s.innerH === 0,
     s && "revealed=" + s.revealed + " innerH=" + s.innerH);

  await p.close();
}

// -------------------------------------------------------------------- router
async function routerChecks(browser, base) {

  // Cold launch with no hash.
  let p = await newPage(browser);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  ok("no hash -> normalises to #/", (await p.evaluate(() => location.hash)) === "#/");
  ok("Who's Hot container visible", await p.evaluate(() => !document.getElementById("app").hidden));
  ok("insights container hidden", await p.evaluate(() => document.getElementById("insightsView").hidden));
  ok("scope class off on the app route",
     !(await p.evaluate(() => document.body.classList.contains("insights-scope"))));
  await p.close();

  // Cold launch on a *valid* inner route must still normalise -- the unknown-hash
  // fallback would happily honour it, so this needs its own check.
  p = await newPage(browser);
  await p.goto(base + "/index.html#/games", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  ok("cold launch on a saved #/games -> #/", (await p.evaluate(() => location.hash)) === "#/",
     await p.evaluate(() => location.hash));
  ok("and Who's Hot is what rendered", await p.evaluate(() => !document.getElementById("app").hidden));
  await p.close();

  // The dev route is exempt: direct URL is its only access path.
  p = await newPage(browser);
  await p.goto(base + "/index.html#/components", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(700);
  ok("cold launch on #/components is honoured",
     (await p.evaluate(() => location.hash)) === "#/components");
  ok("components renders the gallery",
     (await p.$$eval("#insightsRoot .gallery-item", (e) => e.length)) > 0);
  ok("components is absent from the tab bar",
     (await p.$$eval('.tab[data-route="components"]', (e) => e.length)) === 0);
  await p.close();

  // Unknown hash.
  p = await newPage(browser);
  await p.goto(base + "/index.html#/nope", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  ok("unknown hash -> #/", (await p.evaluate(() => location.hash)) === "#/");
  await p.close();

  // Navigation, tab state, and the single-document guarantee.
  p = await newPage(browser);
  const problems = collectProblems(p, base);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  // Pin a value to the window: it survives any same-document navigation and is
  // wiped by a real page load. (framenavigated is no good here -- Playwright
  // fires it for hash changes too.)
  await p.evaluate(() => { window.__sessionMarker = "alive-" + Date.now(); });
  const marker = await p.evaluate(() => window.__sessionMarker);

  ok("tab bar has four tabs", (await p.$$eval(".tab", (e) => e.length)) === 4);

  for (const hash of ROUTES) {
    await p.click('.tab[href="' + hash + '"]');
    await p.waitForTimeout(450);
    const st = await p.evaluate(() => ({
      hash: location.hash,
      appHidden: document.getElementById("app").hidden,
      insHidden: document.getElementById("insightsView").hidden,
      scoped: document.body.classList.contains("insights-scope"),
      current: [].map.call(document.querySelectorAll('.tab[aria-current="page"]'),
                           (t) => t.getAttribute("data-route")),
      rendered: document.querySelectorAll("#insightsRoot > *, #app .wrap").length,
    }));
    const isApp = hash === "#/";
    ok("navigate " + hash, st.hash === hash, st.hash);
    ok("  containers toggle symmetrically", st.appHidden === !isApp && st.insHidden === isApp,
       "app hidden=" + st.appHidden + " insights hidden=" + st.insHidden);
    ok("  scope class matches section", st.scoped === !isApp);
    ok("  exactly one active tab", st.current.length === 1, st.current.join(",") || "none");
    ok("  content rendered", st.rendered > 0, String(st.rendered));
  }

  ok("one document survives every tab switch",
     (await p.evaluate(() => window.__sessionMarker)) === marker,
     "marker " + ((await p.evaluate(() => window.__sessionMarker)) === marker ? "intact" : "lost"));

  // Back / forward through the hash history.
  // The loop ends on #/teams; clicking #/ while already there adds no entry, so
  // the previous entry is #/players.
  await p.goBack(); await p.waitForTimeout(450);
  ok("back returns to the previous route", (await p.evaluate(() => location.hash)) === "#/players",
     await p.evaluate(() => location.hash));
  ok("  and back re-renders that section",
     (await p.$$eval("#insightsRoot > *", (e) => e.length)) > 0);
  await p.goForward(); await p.waitForTimeout(450);
  ok("forward returns again", (await p.evaluate(() => location.hash)) === "#/teams",
     await p.evaluate(() => location.hash));

  // Route changes must reset document scroll.
  await p.click('.tab[href="#/games"]'); await p.waitForTimeout(400);
  await p.evaluate(() => document.documentElement.style.minHeight = "3000px");
  await p.evaluate(() => window.scrollTo(0, 600)); await p.waitForTimeout(150);
  const scrolled = await p.evaluate(() => window.scrollY);
  await p.click('.tab[href="#/teams"]'); await p.waitForTimeout(450);
  ok("route change resets scroll", scrolled > 0 && (await p.evaluate(() => window.scrollY)) === 0,
     scrolled + " -> " + (await p.evaluate(() => window.scrollY)));

  ok("no console errors or 4xx across the session", problems.length === 0,
     problems.join("; ") || "clean");
  await p.close();
}


// ---------------------------------------------------------------- safe area
// viewport-fit=cover only makes env(safe-area-inset-*) resolve to non-zero
// values; it insets nothing by itself. This group emulates a notched device via
// CDP so env() resolves for real, then asserts both ends actually apply it --
// the top was missing at first and put the header under the Dynamic Island,
// with nothing to signal it on any non-notched device or in a browser tab.
const INSET_TOP = 59;      // iPhone 15 Pro Dynamic Island
const INSET_BOTTOM = 34;   // home indicator

async function safeAreaChecks(browser, base) {
  // iPhone 15 Pro logical viewport.
  const p = await newPage(browser, { viewport: { width: 393, height: 852 } });
  const cdp = await p.context().newCDPSession(p);
  await cdp.send("Emulation.setSafeAreaInsetsOverride", {
    insets: { top: INSET_TOP, left: 0, bottom: INSET_BOTTOM, right: 0 },
  });
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(700);

  // Phase 3 made the tab bar a floating pill: inset 14px from the left, right
  // and bottom edges instead of running edge to edge. The safe-area inset moved
  // with it, OUT of the bar's padding-bottom and INTO its bottom offset -- a
  // pill has to move as a whole, where the old bar could pad its labels up and
  // leave its background sitting under the home indicator.
  //
  // So this measures the bar's BOX rather than its padding string. That is a
  // stronger assertion than the one it replaces: padding-bottom: 34px was only
  // ever a proxy for "the bar clears the indicator", and it stopped being a
  // valid proxy the moment the bar left the bottom edge.
  const BAR_H = 62;         // .tabbar height
  const BAR_GAP = 14;       // how far it floats above the bottom edge
  const geom = await p.evaluate(() => {
    const bar = document.querySelector(".tabbar");
    const r = bar.getBoundingClientRect();
    return {
      mainTop: getComputedStyle(document.querySelector(".shell-main")).paddingTop,
      mainBottom: getComputedStyle(document.querySelector(".shell-main")).paddingBottom,
      barH: Math.round(r.height),
      barLeft: Math.round(r.left),
      barGapBelow: Math.round(window.innerHeight - r.bottom),
      viewportW: window.innerWidth,
    };
  });
  ok("shell-main applies the top inset", geom.mainTop === INSET_TOP + "px", geom.mainTop);
  ok("shell-main clears the floating bar + bottom inset",
     geom.mainBottom === (90 + INSET_BOTTOM) + "px", geom.mainBottom);
  ok("tab bar is the floating pill height", geom.barH === BAR_H, geom.barH + "px");
  // The whole point of the offset change: the pill's own bottom edge -- not
  // just its labels -- has to sit above the home indicator.
  ok("tab bar floats clear of the home indicator",
     geom.barGapBelow === BAR_GAP + INSET_BOTTOM,
     "gap=" + geom.barGapBelow + " expected " + (BAR_GAP + INSET_BOTTOM));
  // Inset from the side edges too, on a viewport narrower than the 402px cap.
  ok("tab bar is inset from the side edges",
     geom.barLeft === BAR_GAP, "left=" + geom.barLeft + " expected " + BAR_GAP);

  // Geometry, not just declarations: nothing may render inside either strip.
  const tops = {};
  for (const hash of ROUTES) {
    await goRoute(p, hash);
    const g = await p.evaluate(() => {
      const visible = document.querySelector("#app:not([hidden]), #insightsView:not([hidden])");
      const first = visible && visible.querySelector(".wrap > *");
      const tab = document.querySelector(".tab");
      return {
        firstTop: first ? Math.round(first.getBoundingClientRect().top) : null,
        tabBottom: Math.round(tab.getBoundingClientRect().bottom),
        viewportH: window.innerHeight,
      };
    });
    tops[hash] = g.firstTop;
    ok("top content clears the island on " + hash,
       g.firstTop !== null && g.firstTop >= INSET_TOP, "y=" + g.firstTop + " vs inset " + INSET_TOP);
    ok("  tab labels clear the home indicator",
       g.tabBottom <= g.viewportH - INSET_BOTTOM,
       "bottom=" + g.tabBottom + " vs limit " + (g.viewportH - INSET_BOTTOM));
  }

  // Every route must START AT THE SAME HEIGHT. Clearing the inset is necessary
  // but not sufficient: the insights routes once sat 22px lower than Who's Hot
  // because .insights-h1 kept a top margin meant to separate it from a back link
  // the shell had already removed. Each route passed its own check in isolation,
  // so only comparing them catches it -- switching tabs made the content jump.
  const uniq = [...new Set(Object.values(tops))];
  ok("all routes share the same top offset", uniq.length === 1,
     Object.entries(tops).map(([h, y]) => h + "=" + y).join("  "));

  // The scrim masks the strip so SCROLLED content is not visible behind the
  // status bar. Padding cannot do this -- it only sets where content starts.
  const scrim = await p.evaluate(() => {
    const el = document.querySelector(".status-scrim");
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { height: cs.height, position: cs.position, zIndex: cs.zIndex,
             opaque: cs.backgroundColor !== "rgba(0, 0, 0, 0)" };
  });
  ok("status scrim exists", scrim !== null);
  if (scrim) {
    ok("  covers exactly the top inset", scrim.height === INSET_TOP + "px", scrim.height);
    ok("  is fixed and opaque", scrim.position === "fixed" && scrim.opaque,
       scrim.position + " opaque=" + scrim.opaque);
    // Z-order proof: with hit-testing temporarily enabled the scrim must be the
    // topmost element in the strip, which is what "masks" actually means.
    // pointer-events is none in production so the area stays tappable-through.
    await goRoute(p, "#/games");
    const top = await p.evaluate(() => {
      const el = document.querySelector(".status-scrim");
      el.style.pointerEvents = "auto";
      const hit = document.elementFromPoint(Math.round(window.innerWidth / 2), 30);
      el.style.pointerEvents = "";
      return hit ? hit.className : "(none)";
    });
    ok("  paints above scrolled content", String(top).includes("status-scrim"), String(top));
  }

  // A sticky element pins to the SCROLLPORT, so the container padding above
  // does not move it -- it needs the inset on its own `top`.
  await goRoute(p, "#/");
  await p.evaluate(() => { const r = document.querySelector(".player-row"); if (r) r.click(); });
  await p.waitForTimeout(400);
  await p.evaluate(() => window.scrollTo(0, 400));
  await p.waitForTimeout(300);
  const back = await p.evaluate(() => {
    const el = document.querySelector(".detail-back-row");
    if (!el) return null;
    return { cssTop: getComputedStyle(el).top,
             pinnedAt: Math.round(el.getBoundingClientRect().top) };
  });
  ok("sticky detail back row exists once scrolled", back !== null);
  if (back) {
    ok("  sticky offset is the inset, not 0", back.cssTop === INSET_TOP + "px", back.cssTop);
    ok("  pins clear of the island", back.pinnedAt >= INSET_TOP,
       "y=" + back.pinnedAt + " vs inset " + INSET_TOP);
  }
  await p.close();
}

// ---------------------------------------------------------------- standalone
async function standaloneChecks(browser, base) {
  const p = await newPage(browser);
  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  const meta = await p.evaluate(() => ({
    capable: document.querySelectorAll('meta[name="apple-mobile-web-app-capable"]').length,
    capableValue: (document.querySelector('meta[name="apple-mobile-web-app-capable"]') || {}).content,
    statusBar: document.querySelectorAll('meta[name="apple-mobile-web-app-status-bar-style"]').length,
    title: document.querySelectorAll('meta[name="apple-mobile-web-app-title"]').length,
    themeColor: document.querySelectorAll('meta[name="theme-color"]').length,
    viewportFit: /viewport-fit=cover/.test(
      (document.querySelector('meta[name="viewport"]') || {}).content || ""),
  }));
  ok("exactly one apple-mobile-web-app-capable", meta.capable === 1, String(meta.capable));
  ok("and it is yes", meta.capableValue === "yes", String(meta.capableValue));
  ok("one status-bar-style", meta.statusBar === 1, String(meta.statusBar));
  ok("one app title", meta.title === 1, String(meta.title));
  ok("one theme-color", meta.themeColor === 1, String(meta.themeColor));
  ok("viewport-fit=cover for the safe-area insets", meta.viewportFit);

  // The tab bar must clear the iOS home indicator rather than sit under it.
  //
  // This asserts the RULE references env(safe-area-inset-bottom), not a
  // computed value: there is no inset emulation on this page, so every computed
  // number here is identical whether the env() is present or not, and a
  // computed-value check would pass on a bar that ignores the indicator
  // entirely. (The old check -- paddingBottom !== "" -- was true of any element
  // with any padding, so it never had teeth. safeAreaChecks measures the real
  // geometry with the inset emulated; this one guards the declaration.)
  const reservesInset = await p.evaluate(() => {
    for (const sheet of document.styleSheets) {
      let rules;
      try { rules = sheet.cssRules; } catch { continue; }  // cross-origin (fonts)
      for (const r of rules) {
        if (r.selectorText === ".tabbar") {
          return /env\(\s*safe-area-inset-bottom/.test(r.style.getPropertyValue("bottom"));
        }
      }
    }
    return false;
  });
  ok("tab bar offsets itself by the safe-area inset", reservesInset);
  await p.close();

  // Repo-wide, not just this document. The original bug was five pages each
  // having to declare standalone capability, where one missing tag dropped the
  // user out of the home-screen app into Safari. A single shell means a single
  // declaration -- and the way that regresses is someone adding another HTML
  // entry point, which only a filesystem check can see.
  const shipped = htmlFilesUnder(path.join(REPO, "web")).filter((f) => !EXCLUDED_HTML.has(path.basename(f)));
  const declaring = shipped.filter((f) =>
    /apple-mobile-web-app-capable/.test(fs.readFileSync(f, "utf8")));
  ok("exactly one shipped HTML entry point",
     shipped.length === 1, shipped.map((f) => path.relative(REPO, f)).join(", ") || "none");
  ok("exactly one apple-mobile-web-app-capable in the repo",
     declaring.length === 1, declaring.map((f) => path.relative(REPO, f)).join(", ") || "none");
}

// Neither of these is a shipped entry point: tokens.html is a Phase 1 review
// artefact absent from the workflow's copy list, and __leak.html is this
// suite's own scratch page. Excluded by name so they cannot quietly satisfy or
// break the count -- anything else appearing under web/ is a real new entry
// point and should fail until it is deliberately accounted for.
const EXCLUDED_HTML = new Set(["tokens.html", "__leak.html"]);
function htmlFilesUnder(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) return htmlFilesUnder(full);
    return e.name.endsWith(".html") ? [full] : [];
  });
}

// --------------------------------------------------------------------- groups
// Every group, in run order, and the single source of their names -- the driver
// prints the heading from here rather than each group printing its own. That
// matters for the failure this table exists to survive: a group that throws
// before it can announce itself would otherwise be reported under the PREVIOUS
// group's name, which is exactly backwards when the point is to say which one
// broke.
//
// Groups are independent by construction -- each opens its own page(s) and
// asserts against a freshly loaded app -- so the driver is free to run the rest
// after one of them dies. Adding a group means adding a row here.
// ------------------------------------------------------------- game-only-league
// A league can be fully active and publish NO player leaderboards. cfb is
// exactly that by design -- games and teams, no player boards, because there
// are no player props to bet -- and it broke two things that both failed
// silently.
//
// The picker was built from data.json's `sports` block, which only holds
// leagues WITH leaderboards. So cfb's games and teams shipped in the payload,
// correctly scoped and ready to render, with no control anywhere in the app
// that could select them: the league was unreachable, and nothing threw. The
// keys now come from SP.sport.options, which unions that block with the
// leagues the pipeline named in insights.ui.sport_labels.
//
// The second one DID throw, and only on the tab that has nothing to show:
// picking such a league on Games or Teams and walking back to Who's Hot handed
// renderChipRow an undefined sport. That page is now a named empty state, and
// crucially it leaves the SELECTION alone -- snapping back to the first league
// would silently undo a switch made one tab over.
//
// The cfb rows spliced in here are REAL pipeline output (a captured 2025-11-15
// slate: four games, six team profiles), not hand-written -- the shape of a
// game with no leaderboard league behind it is the thing under test, so it has
// to be the shape the pipeline actually emits. The fixture's own rows are
// tagged mlb on the way past, because scoped() deliberately keeps an UNTAGGED
// row (a single-league payload has nothing to scope), and a two-league payload
// with half its rows untagged is not a payload the pipeline can produce.
async function gameOnlyLeagueChecks(browser, base) {
  const extra = JSON.parse(
    fs.readFileSync(path.join(__dirname, "game_only_league_fixture.json"), "utf8"));
  const p = await newPage(browser);
  const problems = collectProblems(p, base);

  await p.route("**/data.json*", async (route) => {
    const res = await route.fetch();
    const data = JSON.parse(await res.text());
    const home = Object.keys(data.sports || {})[0] || "mlb";
    const ins = data.insights || (data.insights = {});
    ["players", "games", "teams"].forEach((k) => {
      ins[k] = (ins[k] || []).map((r) => Object.assign({}, r, { sport: r.sport || home }));
    });
    ins.games = (ins.games || []).concat(extra.games);
    ins.teams = (ins.teams || []).concat(extra.teams);
    ins.ui = Object.assign({}, ins.ui, {
      sport_labels: Object.assign({}, (ins.ui || {}).sport_labels, extra.sport_labels),
    });
    await route.fulfill({ body: JSON.stringify(data), contentType: "application/json" });
  });

  await p.goto(base + "/index.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(450);
  await goRoute(p, "#/games");

  const offered = () => p.$$eval("#insightsRoot [data-sport]",
    (b) => b.map((x) => x.getAttribute("data-sport")).sort());
  ok("the picker offers the game-only league", (await offered()).includes("cfb"),
     (await offered()).join(","));

  // Default selection is unchanged by any of this: still data.json's first
  // league, which is the one WITH leaderboards.
  const beforeAbbrs = () => p.$$eval("#insightsRoot .gr-row .gr-teams .team-chip",
    (n) => n.map((x) => x.textContent.trim()));
  const cfbAbbrs = extra.games.map((g) => g.home.abbr);
  ok("it is not selected by default", !(await beforeAbbrs()).some((a) => cfbAbbrs.includes(a)));

  // Two taps: the collapsed control is a disclosure, so the first opens it.
  await p.click('#insightsRoot .sport-opt.active');
  await p.click('#insightsRoot [data-sport="cfb"]');
  await p.waitForTimeout(450);

  const shown = await beforeAbbrs();
  ok("selecting it shows its games", shown.length > 0 && shown.some((a) => cfbAbbrs.includes(a)),
     shown.join(",") || "none");
  ok("  and NOTHING from the other league", shown.every((a) => cfbAbbrs.includes(a) ||
     extra.games.some((g) => g.away.abbr === a)), shown.join(","));

  await goRoute(p, "#/teams");
  const teamNames = await p.$$eval("#insightsRoot .ti-name", (n) => n.map((x) => x.textContent.trim()));
  const cfbTeams = extra.teams.map((t) => t.name);
  ok("the selection carries to Teams", teamNames.length > 0 && teamNames.every((n) => cfbTeams.includes(n)),
     teamNames.join(",") || "none");

  // A Pulse computed over a DIFFERENT window than the card implies carries a
  // `qualifier`, and it has to reach the screen: these cards say "Scorching"
  // about programs that have not played a snap this season. Two assertions,
  // because the failure modes are opposite -- the text missing entirely, or
  // the season folded into the band word, which would miss pulseBand()'s
  // lookup table and silently grey out every band colour.
  const quals = extra.teams.filter((t) => t.pulse && t.pulse.qualifier);
  if (quals.length) {
    const labels = await p.$$eval("#insightsRoot .pulse-label", (n) => n.map((x) => x.textContent.trim()));
    ok("  a stale Pulse says which season it is from",
       labels.some((l) => l.includes(quals[0].pulse.qualifier.toUpperCase())
                       || l.includes(quals[0].pulse.qualifier)), labels.slice(0, 2).join(" | "));
    const bands = await p.$$eval("#insightsRoot .pulse", (n) => n.map((x) => x.className));
    ok("  and still resolves its band colour (not the grey fallback)",
       bands.length > 0 && bands.every((c) => !/pulse-cool/.test(c)), bands.slice(0, 2).join(" | "));
  }

  // The tab with nothing to show. It must NAME the league, and must not have
  // quietly reset the selection to get there.
  await goRoute(p, "#/");
  const app = await p.$eval("#app", (n) => n.innerText);
  ok("Who's Hot names the league instead of throwing", /College Football/.test(app),
     app.slice(0, 120).replace(/\n/g, " "));
  ok("  and renders no leaderboard chips", (await p.$$eval("#app .chip", (n) => n.length)) === 0);
  ok("  the selection survived the visit",
     (await p.evaluate(() => window.SP.sport.get())) === "cfb");

  // ...and switching back from that empty page still works, which is the only
  // way out of it.
  await p.click('#app .sport-opt.active');
  await p.click('#app [data-sport]:not(.active)');
  await p.waitForTimeout(450);
  ok("switching back from it restores the boards",
     (await p.$$eval("#app .chip", (n) => n.length)) > 0);

  ok("no errors anywhere in that walk", problems.length === 0, problems.join(" | ") || "clean");
  await p.close();
}

const GROUPS = [
  ["insights-scope-leak-check", scopeLeakCheck],
  ["re-entry", reEntryChecks],
  ["player-detail", playerDetailChecks],
  ["detail-history", detailHistoryChecks],
  ["ai-note", aiNoteChecks],
  ["game-only-league", gameOnlyLeagueChecks],
  ["router", routerChecks],
  ["safe-area", safeAreaChecks],
  ["standalone", standaloneChecks],
];

// ------------------------------------------------------------------- fixtures
// The leak check needs a page with both stylesheets and the markup app.js emits
// for the player detail. Written next to the app so relative hrefs resolve.
const LEAK_PAGE = `<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="app.css">
<link rel="stylesheet" href="insights/insights.css">
<body>
  <div class="vs-starter-section">
    <div class="breakdown-label">Vs next starter</div>
    <div class="vs-starter-line">2-7 &middot; 1 HR</div>
    <div class="vs-starter-caveat">Small sample</div>
  </div>
</body>`;

(async () => {
  if (!fs.existsSync(path.join(WEB, "data.json"))) {
    console.error(path.join(WEB, "data.json") + " missing" +
      (IS_SOURCE_TREE ? " -- run: python3 -m tools.verify.make_fixture" : ""));
    process.exit(2);
  }
  const leakPath = path.join(WEB, "__leak.html");
  fs.writeFileSync(leakPath, LEAK_PAGE);

  const { server, base } = await serve(WEB);
  const browser = await chromium.launch();
  try {
    for (const [name, run] of GROUPS) {
      heading(name);
      try {
        await run(browser, base);
      } catch (e) {
        // A crash is NOT the same as a failed assertion and is not reported as
        // one: a FAIL is one known answer being wrong, a CRASH is an unknown
        // number of this group's remaining assertions never having run. It
        // still counts toward `fail`, so the exit code is unchanged.
        crashed.push(name);
        fail++;
        const why = (e && e.message ? String(e.message) : String(e)).split("\n")[0];
        // The first real stack frame, not stack[1]: a Playwright error folds its
        // whole call log into .stack ahead of the frames, so the naive second
        // line is "Call log:" rather than the line that actually threw -- which
        // is the one thing worth printing here.
        const frame = ((e && e.stack ? e.stack : "").split("\n")
          .find((l) => /^\s*at /.test(l)) || "").trim();
        failures.push(name + " / GROUP CRASHED: " + why);
        console.log("  CRASH " + name + " -- " + why);
        if (frame) console.log("         " + frame);
        console.log("         remaining checks in this group did not run");
      } finally {
        // Groups close their own pages on the way out; one that threw did not.
        // newPage() gives each page its own context, so a leaked page would
        // otherwise stay live -- with its timers, its routes and its console
        // listeners -- inside every group that follows.
        for (const ctx of browser.contexts()) {
          await ctx.close().catch(() => {});
        }
      }
    }
  } finally {
    await browser.close();
    server.close();
    fs.unlinkSync(leakPath);
  }

  console.log("\n" + pass + " passed, " + fail + " failed" +
              (crashed.length ? ", " + crashed.length + " group(s) crashed" : ""));
  if (crashed.length) {
    console.log("crashed: " + crashed.join(", ") +
                " -- those groups are PARTIALLY REPORTED; counts above exclude " +
                "whatever they never reached");
  }
  if (fail) {
    console.log("\nfailures:");
    failures.forEach((f) => console.log("  " + f));
  }
  process.exit(fail ? 1 : 0);
})();
