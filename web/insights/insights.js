(function () {
  "use strict";

  // Insights section -- Phase 2: reusable card UI fed by mock JSON. No backend,
  // no API. The card builders below are pure (data in -> HTML string out),
  // mirroring app.js's rendering style, and are exposed on window.SP.Cards so
  // any page on the site (including the main app later) can reuse them.

  function esc(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // Team-color tint for the reused .team-chip background (matches app.js's
  // alpha() helper: append an 8-bit hex alpha suffix to a #RRGGBB color).
  function alpha(hex, suffix) {
    return hex && hex.charAt(0) === "#" ? hex + suffix : "rgba(255,255,255,0.12)";
  }

  function teamTag(t) {
    if (!t || !t.abbr) return "";
    var c = t.color || "rgba(255,255,255,0.5)";
    // Reuses .team-chip from app.css for consistent chip styling.
    return '<span class="team-chip" style="color:' + c + ";background:" + alpha(c, "26") + '">' + esc(t.abbr) + "</span>";
  }

  function pulseBand(score) {
    return score >= 80 ? "hot" : score >= 60 ? "warm" : "cool";
  }

  // A labeled sub-section wrapper, reusing app.css's .breakdown-label.
  function section(label, inner) {
    if (!inner) return "";
    return '<div class="insight-section"><div class="breakdown-label">' + esc(label) + "</div>" + inner + "</div>";
  }

  // Unlabeled spacing wrapper -- for sub-cards that carry their own header
  // (e.g. the AI Summary), so they aren't given a redundant section label.
  function block(inner) {
    return inner ? '<div class="insight-section">' + inner + "</div>" : "";
  }

  var Cards = {
    // Pulse Score -- a 0..100 "how notable is this right now" gauge. Reusable
    // standalone or embedded in the identity cards below.
    pulseScore: function (p) {
      if (!p) return "";
      var s = Math.max(0, Math.min(100, Number(p.score) || 0));
      return (
        '<div class="pulse pulse-' + pulseBand(s) + '">' +
        '<div class="pulse-score">' + s + '<span class="pulse-max">/100</span></div>' +
        '<div class="pulse-meta">' +
        '<div class="pulse-label">' + esc(p.label || "Pulse") + "</div>" +
        '<div class="pulse-bar"><div class="pulse-fill" style="width:' + s + '%"></div></div>' +
        "</div>" +
        "</div>"
      );
    },

    // Key Signals -- a compact list of labeled metrics, each with a tone
    // (pos/neg/neutral) that colors an up/down/flat marker. tone is the
    // connotation, not the raw direction (e.g. a rising bullpen ERA is "neg").
    keySignals: function (signals) {
      if (!signals || !signals.length) return "";
      var rows = signals
        .map(function (sg) {
          var tone = sg.tone === "pos" ? "pos" : sg.tone === "neg" ? "neg" : "neutral";
          var mark = tone === "pos" ? "▲" : tone === "neg" ? "▼" : "•";
          return (
            '<div class="signal-row">' +
            '<span class="signal-label">' + esc(sg.label) + "</span>" +
            '<span class="signal-value"><span class="signal-mark signal-' + tone + '">' + mark + "</span>" + esc(sg.value) + "</span>" +
            "</div>"
          );
        })
        .join("");
      return '<div class="signals">' + rows + "</div>";
    },

    // AI Summary -- a plain-language explanation block. Carries an AI badge and
    // a standing "context, not a prediction" caveat that anchors the section's
    // purpose. (Phase 2: text is mock; Phase 3 wires the real generator.)
    aiSummary: function (summary, story) {
      if (!summary && !story) return "";
      return (
        '<div class="ai-summary">' +
        '<div class="ai-summary-head"><span class="ai-badge">AI</span><span class="ai-summary-title">Summary</span></div>' +
        (summary ? '<p class="ai-summary-text">' + esc(summary) + "</p>" : "") +
        (story ? '<p class="ai-summary-story">' + esc(story) + "</p>" : "") +
        '<div class="ai-caveat">Context, not a prediction.</div>' +
        "</div>"
      );
    },

    // Game Insight -- composes matchup identity + the three sub-cards.
    gameInsight: function (g) {
      if (!g) return "";
      var away = g.away || {}, home = g.home || {};
      return (
        '<article class="insight-card">' +
        '<div class="ic-head gi-head">' +
        '<div class="gi-teams">' + teamTag(away) + '<span class="gi-at">@</span>' + teamTag(home) + "</div>" +
        (g.start ? '<div class="gi-when">' + esc(g.start) + "</div>" : "") +
        "</div>" +
        (g.venue ? '<div class="gi-venue">' + esc(g.venue) + "</div>" : "") +
        (g.headline ? '<p class="insight-headline">' + esc(g.headline) + "</p>" : "") +
        Cards.pulseScore(g.pulse) +
        section("Key Signals", Cards.keySignals(g.signals)) +
        block(Cards.aiSummary(g.summary)) +
        "</article>"
      );
    },

    // Team Insight -- team identity + the three sub-cards.
    teamInsight: function (t) {
      if (!t) return "";
      return (
        '<article class="insight-card">' +
        '<div class="ic-head ti-head">' + teamTag(t) + '<span class="ti-name">' + esc(t.name) + "</span></div>" +
        (t.headline ? '<p class="insight-headline">' + esc(t.headline) + "</p>" : "") +
        Cards.pulseScore(t.pulse) +
        section("Key Signals", Cards.keySignals(t.signals)) +
        block(Cards.aiSummary(t.summary)) +
        "</article>"
      );
    },

    // Player Insight -- player identity + the three sub-cards.
    playerInsight: function (p) {
      if (!p) return "";
      var sub = esc(p.team || "") + (p.pos ? " &middot; " + esc(p.pos) : "");
      return (
        '<article class="insight-card">' +
        '<div class="ic-head pi-head"><div class="pi-name">' + esc(p.name) + '</div><div class="pi-sub">' + sub + "</div></div>" +
        (p.headline ? '<p class="insight-headline">' + esc(p.headline) + "</p>" : "") +
        Cards.pulseScore(p.pulse) +
        section("Key Signals", Cards.keySignals(p.signals)) +
        block(Cards.aiSummary(p.summary, p.story)) +
        "</article>"
      );
    },
  };

  // Expose for reuse across the site.
  window.SP = window.SP || {};
  window.SP.Cards = Cards;

  // ---- page bootstrap: load mock data, render the current view ----
  var root = document.getElementById("insightsRoot");
  if (!root) return; // static pages (e.g. the hub) have no render target

  var view = document.body.getAttribute("data-insights-view");
  // Players render from the live pipeline output (../data.json -> insights.players);
  // games/teams/components are still the deferred mock.
  var src = view === "players" ? "../data.json" : "mock-insights.json";

  fetch(src, { cache: "no-store" })
    .then(function (r) {
      if (!r.ok) throw new Error("fetch " + src + " " + r.status);
      return r.json();
    })
    .then(function (data) { renderView(view, data, root); })
    .catch(function () {
      root.innerHTML = '<p class="empty-state">Could not load insights.</p>';
    });

  function list(items, fn) {
    return items && items.length ? items.map(fn).join("") : '<p class="empty-state">Nothing to show right now.</p>';
  }

  function renderView(view, data, root) {
    if (view === "players") root.innerHTML = list((data.insights && data.insights.players) || [], Cards.playerInsight);
    else if (view === "games") root.innerHTML = list(data.games, Cards.gameInsight);
    else if (view === "teams") root.innerHTML = list(data.teams, Cards.teamInsight);
    else if (view === "components") root.innerHTML = renderGallery(data);
    else root.innerHTML = "";
  }

  // Component gallery: each of the six card types shown in isolation so they're
  // independently testable. The three sub-cards are wrapped in a bare
  // .insight-card to show how they look standalone.
  function renderGallery(data) {
    var g = (data.games || [])[0], t = (data.teams || [])[0], p = (data.players || [])[0];
    function boxed(inner) { return '<article class="insight-card">' + inner + "</article>"; }
    function item(title, inner) { return '<div class="gallery-item"><div class="gallery-tag">' + esc(title) + "</div>" + inner + "</div>"; }
    return [
      item("Game Insight", Cards.gameInsight(g)),
      item("Team Insight", Cards.teamInsight(t)),
      item("Player Insight", Cards.playerInsight(p)),
      item("Key Signals", boxed(section("Key Signals", Cards.keySignals(p && p.signals)))),
      item("Pulse Score", boxed(Cards.pulseScore(p && p.pulse))),
      item("AI Summary", boxed(Cards.aiSummary(p && p.summary))),
    ].join("");
  }
})();
