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

  // Probable starters line for the game card: NAMES ONLY (the ERA matchup is
  // already a "Probables ERA" row in Key Signals, so names are the non-duplicated
  // info). Degrades to whichever side is announced; omitted when neither is.
  function probablesLine(pr) {
    if (!pr) return "";
    var a = pr.away && pr.away.name, h = pr.home && pr.home.name;
    if (!a && !h) return "";
    var names = a && h ? esc(a) + " vs " + esc(h) : esc(a || h);
    return '<div class="gi-probables">Starters: ' + names + "</div>";
  }

  // Sport-level presentation config (data.insights.ui), populated at render time.
  // Card builders look up UI[entity.sport]; nothing sport-specific is hardcoded.
  var UI = {};

  // Icon registry: name -> inline SVG (CSP-safe, no external assets). Sport
  // config chooses which named icon each category uses -- this is only the glyph
  // library, it carries no labels and no logic. A future sport adds glyphs here
  // and references them by name from its own config. Unknown names fall back to a
  // neutral dot, so an unconfigured icon never breaks the strip.
  var ICONS = {
    mound: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2 15h16M4 15c1.5-3 4-5 6-5s4.5 2 6 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    relief: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 8a5 5 0 0 1 9-2M15 12a5 5 0 0 1-9 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M14 3v3h-3M6 17v-3h3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    bat: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 16l9-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="14.5" cy="5.5" r="2" fill="currentColor"/></svg>',
    _default: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3" fill="currentColor"/></svg>',
  };
  function icon(name) { return ICONS[name] || ICONS._default; }

  // The single neutral accent (used for totals / non-team-specific markets and
  // any element with no one clear associated team).
  var GOLD = "#f0a83a";

  // Resolve a Signal Score / Best Angle row to a team color: markets tied to one
  // team (moneyline "SEA", team_total "TB Over", first-five "TOR") take that
  // team's color; totals / first-five totals / NRFI-YRFI have no single team, so
  // they get the neutral gold. Matching is on the leading token of `side`.
  function sideColor(side, away, home) {
    if (!side) return GOLD;
    var tok = String(side).split(" ")[0];
    if (away && away.abbr === tok && away.color) return away.color;
    if (home && home.abbr === tok && home.color) return home.color;
    return GOLD;
  }

  // "Est. 8 runs (7-10)" -- the deterministic estimate the build attaches to
  // the market it describes (today: game_total). Supplementary context for the
  // Signal Score, never a replacement for it, and never a market line. Empty
  // string when no estimate was attached, so callers render nothing at all.
  function estText(o) {
    if (!o || o.point == null) return "";
    var band = o.low != null && o.high != null ? " (" + o.low + "–" + o.high + ")" : "";
    return "Est. " + o.point + (o.unit ? " " + o.unit : "") + band;
  }

  // "2026-07-07" -> "7/7" for the recent-form bar labels.
  function fmtDate(iso) {
    var m = /^\d{4}-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m ? Number(m[1]) + "/" + Number(m[2]) : "";
  }

  // "2026-07-22" -> "Jul 22" (matches the main app's vs-next-starter title).
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtGameDateLong(iso) {
    var parts = String(iso || "").split("-");
    if (parts.length !== 3) return "";
    var m = Number(parts[1]), day = Number(parts[2]);
    return MONTHS[m - 1] && day ? MONTHS[m - 1] + " " + day : "";
  }

  // Recent-form eyebrow: "{STAT} · Last n G" (CSS uppercases it), matching the
  // main app. STAT is the label of the same best-rank category the series came
  // from. Falls back to a plain label if the category label or series is missing.
  function formEyebrow(p) {
    var n = (p.series || []).length;
    return p.series_label && n ? p.series_label + " · Last " + n + " G" : "Recent Form";
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

    // Category strip -- a compact icon+label row of the signal categories a sport
    // tracks. Fully data-driven from data.insights.ui[sport].signal_categories;
    // the component supplies only the glyph library (ICONS). Empty when a sport
    // has no configured categories (e.g. an unconfigured future sport).
    categoryStrip: function (cats) {
      if (!cats || !cats.length) return "";
      var items = cats
        .map(function (c) {
          return (
            '<div class="cat-item">' +
            '<span class="cat-icon">' + icon(c.icon) + "</span>" +
            '<span class="cat-label">' + esc(c.label) + "</span>" +
            "</div>"
          );
        })
        .join("");
      return '<div class="category-strip">' + items + "</div>";
    },

    // Signal Scores -- a ranked list of 0-100 computed signal scores per market.
    // Generic: renders whatever {market, side, score} rows it's handed (already
    // ranked by the build). The row matching `bestAngle` is flagged. Empty when
    // there are no material signals.
    signalScores: function (scores, bestAngle, away, home) {
      if (!scores || !scores.length) return "";
      var baKey = bestAngle ? bestAngle.market + "|" + bestAngle.side : null;
      var rank = 0;
      var rows = scores
        .filter(function (s) { return !(baKey && s.market + "|" + s.side === baKey); })
        .map(function (s) {
          rank += 1;
          var pct = Math.max(0, Math.min(100, Number(s.score) || 0));
          var color = sideColor(s.side, away, home);
          return (
            '<div class="ss-row" style="--row-color:' + esc(color) + '">' +
            '<span class="ss-rank">' + rank + "</span>" +
            '<div class="ss-main">' +
            '<div class="ss-market">' + esc(s.market) + "</div>" +
            '<div class="ss-side">' + esc(s.side) + "</div>" +
            (estText(s) ? '<div class="ss-est">' + esc(estText(s)) + "</div>" : "") +
            "</div>" +
            '<div class="ss-scorebox"><div class="ss-score">' + pct + "</div>" +
            '<div class="ss-scorelabel">Score</div></div>' +
            "</div>"
          );
        })
        .join("");
      if (!rows) return "";
      return (
        '<div class="signal-scores">' + rows +
        '<div class="disclaimer">Computed indicators (0–100) from recent form &amp; matchup — not win probabilities.</div>' +
        "</div>"
      );
    },

    // Best Angle -- the single standout market, promoted out of the ranked list
    // into a larger tinted card (market's team color if it has one, else gold).
    bestAngle: function (ba, away, home) {
      if (!ba) return "";
      var pct = Math.max(0, Math.min(100, Number(ba.score) || 0));
      var color = sideColor(ba.side, away, home);
      return (
        '<div class="best-angle" style="--ba-color:' + esc(color) + '">' +
        '<span class="ba-tag">Best Angle</span>' +
        '<div class="ba-row">' +
        "<div><div class=\"ba-market\">" + esc(ba.market || "") + "</div>" +
        '<div class="ba-side">' + esc(ba.side || "") + "</div>" +
        (estText(ba) ? '<div class="ba-est">' + esc(estText(ba)) + "</div>" : "") +
        "</div>" +
        '<div class="ba-scorebox"><div class="ba-score">' + pct + "</div>" +
        '<div class="ba-scorelabel">Score</div></div>' +
        "</div>" +
        "</div>"
      );
    },

    // Compare Metrics -- a generic "compare N metrics between two entities" table.
    // Knows nothing about what the metrics are: it renders resolved rows
    // ({label, a, b, better}) and bolds the winning side per row. The metric list
    // and which side wins are decided by the sport-aware build. Empty when there
    // are no rows (e.g. an unannounced entity, or no metric data yet).
    compareMetrics: function (c) {
      if (!c || !c.rows || !c.rows.length) return "";
      var a = (c.a && c.a.name) || "", b = (c.b && c.b.name) || "";
      var head =
        '<div class="cmp-row cmp-head">' +
        '<span class="cmp-metric"></span>' +
        '<span class="cmp-val">' + esc(a) + "</span>" +
        '<span class="cmp-val">' + esc(b) + "</span></div>";
      var rows = c.rows
        .map(function (r) {
          return (
            '<div class="cmp-row">' +
            '<span class="cmp-metric">' + esc(r.label) + "</span>" +
            '<span class="cmp-val' + (r.better === "a" ? " cmp-win" : "") + '">' + esc(r.a) + "</span>" +
            '<span class="cmp-val' + (r.better === "b" ? " cmp-win" : "") + '">' + esc(r.b) + "</span>" +
            "</div>"
          );
        })
        .join("");
      return '<div class="compare-table">' + head + rows + "</div>";
    },

    // Recent Form -- a HORIZONTAL bar chart (leaderboard convention): one bar per
    // data point, proportional width, value at the end, date beneath. Bar fill is
    // the entity's team color. Generic over any [{value, date}] series; empty when
    // there's no series.
    formBars: function (series, color) {
      if (!series || !series.length) return "";
      var vals = series.map(function (s) { return Number(s && s.value) || 0; });
      var max = Math.max.apply(null, vals.concat([1])); // guard divide-by-zero
      var fill = color || GOLD;
      var rows = series
        .map(function (s) {
          var v = Number(s && s.value) || 0;
          var w = Math.max(2, Math.round((v / max) * 100));
          var d = fmtDate(s && s.date);
          return (
            '<div class="fb-item">' +
            '<div class="fb-row">' +
            '<div class="fb-track"><div class="fb-bar" style="width:' + w + "%;--fb-color:" + esc(fill) + '"></div></div>' +
            '<span class="fb-val">' + v + "</span>" +
            "</div>" +
            (d ? '<div class="fb-date">' + esc(d) + "</div>" : "") +
            "</div>"
          );
        })
        .join("");
      return '<div class="form-bars">' + rows + "</div>";
    },

    // Vs Next Starter -- the player's career line against today's probable
    // pitcher, mirroring the main app's block. Null (no starter announced yet, or
    // no head-to-head history) renders nothing. Small-sample caveat under 10 AB.
    vsStarter: function (vs) {
      if (!vs) return "";
      var date = fmtGameDateLong(vs.game_date);
      var title = "Vs next starter — " + esc(vs.pitcher_name) + (date ? " (" + esc(date) + ")" : "");
      var line =
        Number(vs.hits) + "-" + Number(vs.ab) +
        " · " + Number(vs.hr) + " HR" +
        " · " + Number(vs.rbi) + " RBI" +
        (vs.avg ? " · " + esc(vs.avg) + " AVG" : "");
      var caveat = Number(vs.ab) < 10
        ? '<div class="vs-starter-caveat">Small sample · ' + Number(vs.ab) + " career AB</div>"
        : "";
      return (
        '<div class="vs-starter-section">' +
        '<div class="breakdown-label">' + title + "</div>" +
        '<div class="vs-starter-line">' + line + "</div>" +
        caveat +
        "</div>"
      );
    },

    // Run Estimate -- a deterministic implied game-total. NOT AI and NOT a market
    // line: `point` (nearest whole run) is the headline number; the +/-1sigma
    // `low`-`high` band renders smaller beneath, and the not-a-line `note` stays
    // attached to that range (never the headline). Empty string when there's no
    // estimate (unannounced starter) -- same empty-state discipline as the notes.
    estTotal: function (e) {
      if (!e || e.point == null) return "";
      var unit = e.unit || "";
      var hasBand = e.low != null && e.high != null && e.low !== e.high;
      var band = hasBand ? esc(e.low) + "–" + esc(e.high) + (unit ? " " + esc(unit) : "") : "";
      return (
        '<div class="est-total">' +
        '<div class="est-headline">Est. ' + esc(e.point) + ' <span class="est-unit">' + esc(unit) + "</span></div>" +
        '<div class="est-range">' +
        (band ? '<span class="est-band">Range ' + band + "</span>" : "") +
        (e.note ? '<button class="info-btn" type="button" data-toggle aria-label="About this estimate">i</button>' : "") +
        (e.note ? '<span class="est-note" hidden>' + esc(e.note) + "</span>" : "") +
        "</div>" +
        "</div>"
      );
    },

    // AI Summary -- a plain-language explanation block. Carries an AI badge and
    // a standing "context, not a prediction" caveat that anchors the section's
    // purpose. Optional `note` ({label, text}) renders a small labeled line
    // beneath the story (game betting_note / player matchup_note) -- shown only
    // when non-empty, and sitting inside this block so the caveat covers it too.
    aiSummary: function (summary, story, note) {
      var hasNote = note && note.text;
      if (!summary && !story && !hasNote) return "";
      return (
        '<div class="ai-summary' + (story ? " clamp" : "") + '">' +
        '<div class="ai-summary-head"><span class="ai-badge">AI Note</span></div>' +
        (summary ? '<p class="ai-summary-text">' + esc(summary) + "</p>" : "") +
        (story ? '<p class="ai-summary-story">' + esc(story) + "</p>" : "") +
        (story ? '<button class="ai-readmore" type="button" data-readmore>Read full note →</button>' : "") +
        (hasNote ? '<div class="ai-note"><span class="ai-note-label">' + esc(note.label) + "</span>" + esc(note.text) + "</div>" : "") +
        '<div class="ai-caveat">Context, not a prediction.</div>' +
        "</div>"
      );
    },

    // Game Row -- the compact COLLAPSED-state row for the games list: matchup +
    // start time, up to three ranked market chips, and a small Pulse number.
    // Deliberately much lighter than gameInsight (no gauge, no sub-cards, no AI
    // text); the two are siblings, not variants -- gameInsight stays the
    // expanded view. Pure function, not yet wired into the games view.
    //
    // Class names are all "gr-" prefixed hooks for the follow-up CSS piece, so
    // this row carries no dependency on the existing gi-/ss-/ba- styles and the
    // two can be restyled independently.
    gameRow: function (g) {
      if (!g) return "";
      var away = g.away || {}, home = g.home || {};
      var ba = g.best_angle;
      // Best Angle leads; the ranked signal_scores fill the remaining slots.
      // Same market+side dedupe key Cards.signalScores uses, so the Best Angle
      // never also appears as a plain chip. (best_angle carries both bet_type
      // and the readable `market` label; signal_scores rows carry `market` --
      // so `market` is the field the two genuinely share.)
      var baKey = ba ? ba.market + "|" + ba.side : null;
      var picks = (ba ? [ba] : []).concat(
        (g.signal_scores || []).filter(function (s) {
          return !(baKey && s.market + "|" + s.side === baKey);
        })
      ).slice(0, 3);
      // No chips at all when a game has neither -- no placeholder by design.
      var chips = picks
        .map(function (s) {
          var pct = Math.max(0, Math.min(100, Number(s.score) || 0));
          return (
            '<span class="gr-chip" style="--chip-color:' + esc(sideColor(s.side, away, home)) + '">' +
            '<span class="gr-chip-market">' + esc(s.market || "") + "</span>" +
            '<span class="gr-chip-side">' + esc(s.side || "") + "</span>" +
            '<span class="gr-chip-score">' + pct + "</span>" +
            "</span>"
          );
        })
        .join("");
      // Secondary Pulse: the number and its band color only. Reuses pulseBand()
      // rather than Cards.pulseScore() -- the gauge belongs to the expanded card.
      var pulse = "";
      if (g.pulse && g.pulse.score != null) {
        var ps = Math.max(0, Math.min(100, Number(g.pulse.score) || 0));
        pulse = '<span class="gr-pulse gr-pulse-' + pulseBand(ps) + '">' + ps + "</span>";
      }
      return (
        '<div class="gr-row">' +
        '<div class="gr-head">' +
        '<div class="gr-teams">' + teamTag(away) + '<span class="gr-at">@</span>' + teamTag(home) + "</div>" +
        (g.start ? '<div class="gr-when">' + esc(g.start) + "</div>" : "") +
        "</div>" +
        (chips ? '<div class="gr-chips">' + chips + "</div>" : "") +
        pulse +
        "</div>"
      );
    },

    // Game Insight -- composes matchup identity + the sub-cards. The category
    // strip + comparison title come from sport config (via data), so nothing
    // sport-specific is named in this component.
    gameInsight: function (g) {
      if (!g) return "";
      var away = g.away || {}, home = g.home || {};
      var ui = UI[g.sport] || {};
      return (
        '<article class="insight-card">' +
        '<div class="ic-head gi-head">' +
        '<div class="gi-teams">' + teamTag(away) + '<span class="gi-at">@</span>' + teamTag(home) + "</div>" +
        (g.start ? '<div class="gi-when">' + esc(g.start) + "</div>" : "") +
        "</div>" +
        (g.venue ? '<div class="gi-venue">' + esc(g.venue) + "</div>" : "") +
        probablesLine(g.probables) +
        (g.headline ? '<p class="insight-headline">' + esc(g.headline) + "</p>" : "") +
        Cards.categoryStrip(ui.signal_categories) +
        Cards.pulseScore(g.pulse) +
        section("Key Signals", Cards.keySignals(g.signals)) +
        Cards.bestAngle(g.best_angle, away, home) +
        section("Signal Scores", Cards.signalScores(g.signal_scores, g.best_angle, away, home)) +
        section((g.compare && g.compare.title) || "Comparison", Cards.compareMetrics(g.compare)) +
        section((g.est_total && g.est_total.label) || "Estimate", Cards.estTotal(g.est_total)) +
        block(Cards.aiSummary(g.summary, g.story, g.betting_note ? { label: "Betting signal", text: g.betting_note } : null)) +
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
      var color = p.team_color || GOLD;
      var sub = '<span class="pi-team">' + esc(p.team || "") + "</span>" + (p.pos ? " &middot; " + esc(p.pos) : "");
      return (
        '<article class="insight-card" style="--pi-color:' + esc(color) + '">' +
        '<div class="ic-head pi-head"><div class="pi-name">' + esc(p.name) + '</div><div class="pi-sub">' + sub + "</div></div>" +
        (p.headline ? '<p class="insight-headline">' + esc(p.headline) + "</p>" : "") +
        Cards.pulseScore(p.pulse) +
        section("Key Signals", Cards.keySignals(p.signals)) +
        section(formEyebrow(p), Cards.formBars(p.series, p.team_color)) +
        Cards.vsStarter(p.vs_next_starter) +
        block(Cards.aiSummary(p.summary, p.story, p.matchup_note ? { label: "Matchup", text: p.matchup_note } : null)) +
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

  // Games list state: the rendered slate, keyed by id, so a row can build its
  // expanded card on demand without re-fetching data.json.
  var gamesById = {};

  // One game in the list: the compact row plus its (initially empty) detail
  // panel. The wrapper -- not Cards.gameRow -- owns the id and the panel, so
  // the row component stays the pure standalone function it was built as.
  function gameItem(g) {
    return (
      '<div class="gr-item" data-game-id="' + esc(g && g.id) + '">' +
      Cards.gameRow(g) +
      '<div class="gr-detail"><div class="gr-detail-inner"></div></div>' +
      "</div>"
    );
  }

  function closeItem(item) {
    item.classList.remove("is-open");
    var r = item.querySelector(".gr-row");
    if (r) r.setAttribute("aria-expanded", "false");
  }

  // Accordion: opening a row closes whichever was open, so the list always
  // collapses back to a scannable column instead of stacking several full
  // cards. The expanded card is rendered once, on first open, and kept.
  function toggleGame(row) {
    var item = row.parentNode;
    var isOpen = item.classList.contains("is-open");
    var cur = root.querySelector(".gr-item.is-open");
    if (cur && cur !== item) closeItem(cur);
    if (isOpen) return closeItem(item);
    var inner = item.querySelector(".gr-detail-inner");
    if (inner && !inner.firstChild) {
      inner.innerHTML = Cards.gameInsight(gamesById[item.getAttribute("data-game-id")]);
    }
    item.classList.add("is-open");
    row.setAttribute("aria-expanded", "true");
  }

  // Light affordances (delegated, survives re-render): a game row expands its
  // full card; "i" reveals a hidden disclaimer sibling; "Read full note"
  // un-clamps the AI story. The row check is first and returns early -- the
  // data-toggle controls all live inside .gr-detail, never inside .gr-row, so
  // the two never contend for the same click.
  root.addEventListener("click", function (ev) {
    var row = ev.target.closest && ev.target.closest(".gr-row");
    if (row) return toggleGame(row);
    var t = ev.target.closest && ev.target.closest("[data-toggle],[data-readmore]");
    if (!t) return;
    if (t.hasAttribute("data-readmore")) {
      var card = t.closest(".ai-summary");
      if (card) t.textContent = card.classList.toggle("clamp") ? "Read full note →" : "Show less ←";
    } else {
      var tgt = t.nextElementSibling;
      if (tgt) tgt.hidden = !tgt.hidden;
    }
  });

  // Rows are exposed as buttons (see renderView), so honour the keys a button
  // responds to. preventDefault stops Space from page-scrolling instead.
  root.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
    var row = ev.target.closest && ev.target.closest(".gr-row");
    if (!row) return;
    ev.preventDefault();
    toggleGame(row);
  });

  var view = document.body.getAttribute("data-insights-view");
  // Players and games render from the live pipeline output (../data.json ->
  // insights.players / insights.games); teams/components are still the deferred mock.
  var src = (view === "players" || view === "games") ? "../data.json" : "mock-insights.json";

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
    // Load sport-level presentation config once, before rendering any card.
    UI = (data.insights && data.insights.ui) || {};
    if (view === "players") root.innerHTML = list((data.insights && data.insights.players) || [], Cards.playerInsight);
    else if (view === "games") renderGames((data.insights && data.insights.games) || [], root);
    else if (view === "teams") root.innerHTML = list(data.teams, Cards.teamInsight);
    else if (view === "components") root.innerHTML = renderGallery(data);
    else root.innerHTML = "";
  }

  // Games render as compact rows that expand in place. Every visit starts fully
  // collapsed by design: the slate turns over daily, so a remembered open row
  // would not be resuming anything -- you are re-scanning fresh games.
  function renderGames(games, root) {
    gamesById = {};
    games.forEach(function (g) { if (g && g.id != null) gamesById[String(g.id)] = g; });
    root.innerHTML = list(games, gameItem);
    // Interactive rows, exposed to keyboard and assistive tech here rather than
    // inside Cards.gameRow -- the component stays presentational, and only the
    // wiring that actually makes rows clickable claims they are buttons.
    [].forEach.call(root.querySelectorAll(".gr-row"), function (r) {
      r.setAttribute("role", "button");
      r.setAttribute("tabindex", "0");
      r.setAttribute("aria-expanded", "false");
    });
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
