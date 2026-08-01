(function () {
  "use strict";

  // Navigation shell: hash router + tab bar.
  //
  // Loads after app.js and insights.js, both of which have registered
  // { mount, unmount } on window.SP.views by the time this runs. This file
  // never reaches into either section's internals -- it only decides which one
  // is on screen and tells it to mount.
  //
  // Hash routing rather than the History API because this deploys to GitHub
  // Pages, which has no rewrite/fallback configuration: /games would 404 on
  // refresh or deep link, and the usual 404.html shim costs a visible redirect
  // and a polluted history. The hash also keeps the document base URL fixed, so
  // every relative path in the app resolves the same on every route.

  var SP = window.SP || (window.SP = {});
  var views = SP.views || {};

  // ---------------- location adapter ----------------
  // Every read and write of the URL goes through here. Swapping hash routing
  // for the History API (if this ever moves to a host with SPA fallback --
  // Netlify, Cloudflare, Vercel all do it in a few lines of config) means
  // replacing this object, not touching the router below.
  var loc = {
    read: function () {
      return window.location.hash || "";
    },
    write: function (hash, replace) {
      if (replace) {
        // replaceState does not fire hashchange, which is what we want for
        // normalisation: the router applies the route itself, rather than
        // being re-entered by an event it caused.
        window.history.replaceState(null, "", hash);
      } else {
        window.location.hash = hash;
      }
    },
    subscribe: function (fn) {
      window.addEventListener("hashchange", fn);
    },
  };

  // ---------------- route table ----------------
  // `section` selects the container and the view module; `view` is passed
  // straight through to insights.js's mount(). `title`/`note` are shell chrome:
  // the standalone insights pages each carried their own <h1> and note, and the
  // shell owns that now so insights.js stays purely a content renderer.
  var DEFAULT_HASH = "#/";
  var ROUTES = [
    { hash: "#/", id: "whos-hot", label: "Who's Hot", section: "app", tab: true },
    {
      hash: "#/games", id: "games", label: "Games", tab: true,
      section: "insights", view: "games",
      title: "Today's Games", note: "Today's MLB slate — updated each morning.",
    },
    {
      hash: "#/players", id: "players", label: "Players", tab: true,
      section: "insights", view: "players",
      title: "Players", note: "AI-assisted context, refreshed with each data update.",
    },
    {
      hash: "#/teams", id: "teams", label: "Teams", tab: true,
      section: "insights", view: "teams",
      title: "Teams", note: "Team form for today's slate — 14-day OPS and 7-day bullpen ERA.",
    },
    // Development scaffolding, deliberately absent from the tab bar -- the same
    // "reach it by direct URL only" arrangement components.html has always had.
    {
      hash: "#/components", id: "components", label: "Components", tab: false,
      section: "insights", view: "components",
      title: "Card gallery", note: "Each reusable card in isolation — mock data.",
    },
  ];

  function match(hash) {
    // Tolerate a trailing slash and an empty hash landing on the default.
    var h = (hash || "").replace(/\/$/, "") || "#";
    for (var i = 0; i < ROUTES.length; i++) {
      var r = ROUTES[i];
      if (r.hash === hash || r.hash.replace(/\/$/, "") === h) return r;
    }
    return null;
  }

  // ---------------- DOM handles ----------------
  var appEl = document.getElementById("app");
  var insightsEl = document.getElementById("insightsView");
  var titleEl = document.getElementById("insightsTitle");
  var noteEl = document.getElementById("insightsNote");
  var tabbar = document.getElementById("tabbar");

  var CONTAINERS = { app: appEl, insights: insightsEl };
  var MODULES = { app: "whosHot", insights: "insights" };

  var current = null;

  // ---------------- tab bar ----------------
  // Built once. Active state is re-derived from the current route on every
  // navigation rather than tracked separately, so it cannot drift out of sync.
  function buildTabs() {
    var html = "";
    for (var i = 0; i < ROUTES.length; i++) {
      var r = ROUTES[i];
      if (!r.tab) continue;
      html += '<a class="tab" href="' + r.hash + '" data-route="' + r.id + '">' + r.label + "</a>";
    }
    tabbar.innerHTML = html;
  }

  function markActiveTab(route) {
    var tabs = tabbar.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      var isCurrent = tabs[i].getAttribute("data-route") === route.id;
      if (isCurrent) tabs[i].setAttribute("aria-current", "page");
      else tabs[i].removeAttribute("aria-current");
    }
  }

  // ---------------- navigation ----------------
  function apply(route) {
    // Leaving a section: let it reset whatever a full page load used to reset.
    if (current && current.section !== route.section) {
      var leaving = views[MODULES[current.section]];
      if (leaving && leaving.unmount) leaving.unmount();
    }

    // Token scope. On <body> rather than a descendant so `body { background }`
    // re-evaluates too -- see the header comment in insights.css.
    document.body.classList.toggle("insights-scope", route.section === "insights");

    // Symmetric [hidden] toggle (app.css:30). Both containers are permanent:
    // hidden and emptied, never removed, which is what lets both modules keep
    // their listeners bound from load.
    CONTAINERS.app.hidden = route.section !== "app";
    CONTAINERS.insights.hidden = route.section !== "insights";

    if (route.section === "insights") {
      titleEl.textContent = route.title;
      noteEl.textContent = route.note;
    }

    document.title = route.section === "app" ? "Who's Hot" : route.title + " — Who's Hot";

    markActiveTab(route);
    current = route;

    var mod = views[MODULES[route.section]];
    if (mod && mod.mount) mod.mount(route.view);
  }

  function onHashChange() {
    var route = match(loc.read());
    if (!route) {
      // Unknown hash: normalise to the default and apply it. replaceState keeps
      // the bad URL out of history, so Back does not land on it again.
      loc.write(DEFAULT_HASH, true);
      route = match(DEFAULT_HASH);
    }
    apply(route);
  }

  function start() {
    buildTabs();

    var initial = match(loc.read());

    // Cold launch normalises to the default tab. A bookmarked or saved #/games
    // is a *valid* route, so the unknown-hash fallback above would happily
    // honour it -- this is a separate, deliberate check. iOS relaunches a
    // home-screen app at whatever URL it was saved with, and reopening the app
    // days later onto a stale inner tab is not what "launch the app" should do.
    //
    // #/components is exempt. Direct URL is its only access path -- it is not in
    // the tab bar and nothing links to it -- so normalising it away would make
    // the dev route unreachable rather than merely inconvenient.
    if (!initial || initial.id !== "components") initial = match(DEFAULT_HASH);

    // Always write it back, so the address bar agrees with what is on screen
    // even when the document was opened with no hash at all.
    loc.write(initial.hash, true);
    apply(initial);

    loc.subscribe(onHashChange);
  }

  start();
})();
