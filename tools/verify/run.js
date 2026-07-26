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
 * Playwright is resolved from node_modules if present, otherwise from the
 * global install. This repo intentionally has no package.json; wiring the suite
 * into CI is a separate decision from having it runnable.
 */

"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const WEB = path.join(REPO, "web");

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
    // 404s that would be real in production.
    const base = rel.startsWith("/assets/") ? REPO : root;
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

function heading(name) { group = name; console.log("\n" + name); }
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("  PASS  " + name + (detail ? "  (" + detail + ")" : "")); }
  else { fail++; failures.push(group + " / " + name + (detail ? ": " + detail : ""));
         console.log("  FAIL  " + name + (detail ? "  (" + detail + ")" : "")); }
}

// Fonts are remote; blocking them keeps the suite offline-deterministic and
// stops networkidle waits hanging on a host that cannot reach Google.
async function newPage(browser, opts) {
  const p = await browser.newPage(Object.assign({ viewport: { width: 430, height: 900 } }, opts));
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
  heading("insights-scope-leak-check");
  const p = await newPage(browser);
  // "load", not "domcontentloaded": computed styles are only meaningful once
  // the stylesheets have applied.
  await p.goto(base + "/__leak.html", { waitUntil: "load" });
  await p.waitForTimeout(200);

  const read = () => p.evaluate(() => {
    const g = (sel, prop) => getComputedStyle(document.querySelector(sel))[prop];
    return {
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      breakdownLabelFont: g(".breakdown-label", "fontFamily"),
      breakdownLabelMargin: g(".breakdown-label", "marginBottom"),
      vsStarterLineFont: g(".vs-starter-line", "fontFamily"),
      vsStarterSectionMargin: g(".vs-starter-section", "marginTop"),
      vsStarterCaveatSpacing: g(".vs-starter-caveat", "letterSpacing"),
    };
  });

  const APP = {  // app.css -- what Who's Hot must keep
    bodyBackground: "rgb(10, 10, 11)",
    breakdownLabelFont: '"JetBrains Mono"',
    breakdownLabelMargin: "4px",
    vsStarterLineFont: '"JetBrains Mono"',
    vsStarterSectionMargin: "22px",
    vsStarterCaveatSpacing: "0.5px",
  };
  const INSIGHTS = {  // insights.css -- what the insights section must get
    bodyBackground: "rgb(12, 13, 16)",
    breakdownLabelFont: '"Space Grotesk"',
    breakdownLabelMargin: "10px",
    vsStarterLineFont: '"Space Grotesk"',
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
  await p.close();
}

// ------------------------------------------------------------------ re-entry
async function reEntryChecks(browser, base) {
  heading("re-entry");
  const p = await newPage(browser);
  let json = [];
  p.on("request", (r) => { if (/\.json/.test(r.url())) json.push(r.url().replace(base, "")); });
  await p.goto(base + "/shell.html", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(700);

  ok("leaderboard renders on load", (await p.$$eval(".player-row", (e) => e.length)) > 0);
  ok("one data fetch on load", json.length === 1, json.join(","));

  // Detail -> leave section -> return must land on the list, as a reload does.
  await p.click(".player-row");
  await p.waitForTimeout(250);
  const inDetail = await p.evaluate(() => !!document.querySelector(".hero-value"));
  ok("player detail opens", inDetail);
  json = [];
  await p.evaluate(() => { SP.views.whosHot.unmount(); return SP.views.whosHot.mount(); });
  await p.waitForTimeout(300);
  ok("unmount+mount returns to the list",
     (await p.$$eval(".player-row", (e) => e.length)) > 0 &&
     !(await p.evaluate(() => !!document.querySelector(".hero-value"))));
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
  await p.goto(base + "/shell.html", { waitUntil: "domcontentloaded" });
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

// -------------------------------------------------------------------- router
async function routerChecks(browser, base) {
  heading("router");

  // Cold launch with no hash.
  let p = await newPage(browser);
  await p.goto(base + "/shell.html", { waitUntil: "domcontentloaded" });
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
  await p.goto(base + "/shell.html#/games", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  ok("cold launch on a saved #/games -> #/", (await p.evaluate(() => location.hash)) === "#/",
     await p.evaluate(() => location.hash));
  ok("and Who's Hot is what rendered", await p.evaluate(() => !document.getElementById("app").hidden));
  await p.close();

  // The dev route is exempt: direct URL is its only access path.
  p = await newPage(browser);
  await p.goto(base + "/shell.html#/components", { waitUntil: "domcontentloaded" });
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
  await p.goto(base + "/shell.html#/nope", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(600);
  ok("unknown hash -> #/", (await p.evaluate(() => location.hash)) === "#/");
  await p.close();

  // Navigation, tab state, and the single-document guarantee.
  p = await newPage(browser);
  const problems = collectProblems(p, base);
  await p.goto(base + "/shell.html", { waitUntil: "domcontentloaded" });
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

// ---------------------------------------------------------------- standalone
async function standaloneChecks(browser, base) {
  heading("standalone");
  const p = await newPage(browser);
  await p.goto(base + "/shell.html", { waitUntil: "domcontentloaded" });
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
  const padded = await p.evaluate(() =>
    getComputedStyle(document.querySelector(".tabbar")).paddingBottom !== "");
  ok("tab bar reserves safe-area padding", padded);
  await p.close();
}

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
    console.error("web/data.json missing -- run: python3 -m tools.verify.make_fixture");
    process.exit(2);
  }
  const leakPath = path.join(WEB, "__leak.html");
  fs.writeFileSync(leakPath, LEAK_PAGE);

  const { server, base } = await serve(WEB);
  const browser = await chromium.launch();
  try {
    await scopeLeakCheck(browser, base);
    await reEntryChecks(browser, base);
    await routerChecks(browser, base);
    await standaloneChecks(browser, base);
  } finally {
    await browser.close();
    server.close();
    fs.unlinkSync(leakPath);
  }

  console.log("\n" + pass + " passed, " + fail + " failed");
  if (fail) {
    console.log("\nfailures:");
    failures.forEach((f) => console.log("  " + f));
  }
  process.exit(fail ? 1 : 0);
})();
