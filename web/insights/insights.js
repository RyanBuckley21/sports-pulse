(function () {
  "use strict";

  // Insights section -- Phase 2: reusable card UI fed by mock JSON. No backend,
  // no API. The card builders below are pure (data in -> HTML string out),
  // mirroring app.js's rendering style, and are exposed on window.SP.Cards so
  // any page on the site (including the main app later) can reuse them.

  var SP = window.SP || (window.SP = {});

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
    // `team_color` is the pipeline's key (team profiles, player rows); `color`
    // is the game TeamRef's and the deferred mock's. Both shapes reach this one
    // helper, so it reads either rather than making callers normalise -- and
    // prefers team_color, so a real profile never falls back to the grey.
    var c = t.team_color || t.color || "rgba(255,255,255,0.5)";
    // Reuses .team-chip from app.css for consistent chip styling.
    return '<span class="team-chip" style="color:' + c + ";background:" + alpha(c, "26") + '">' + esc(t.abbr) + "</span>";
  }

  // The band a Pulse belongs to is decided ONCE, in Python (pulse.py), and
  // travels on the pulse object as `label`. This maps that word to a CSS
  // suffix; it does not re-derive the band from the score.
  //
  // That distinction is the whole point. This function used to cut at 80/60
  // while Python's ladder cut at 85/70/55, so the two disagreed over two whole
  // ranges: a 58 read "Warm" in text while colouring itself `cool`, and a 74
  // read "Hot" while colouring itself `warm`. Any future band added to
  // pulse.PULSE_BANDS needs one entry here and nothing else.
  var PULSE_CLASS = {
    Scorching: "hot",
    Hot: "hot",
    Warm: "warm",
    Notable: "cool",
    Cold: "cold",
  };

  // Takes the pulse OBJECT, not a score -- the label is the input now.
  // An unrecognised or absent label falls back to `cool`, the neutral band:
  // the one colour that makes no claim about whether the number is good.
  function pulseBand(p) {
    return (p && PULSE_CLASS[p.label]) || "cool";
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

  // AI Insights feature flag (data.aiInsightsEnabled, set by generate_stats.py
  // from config.yaml's ai_insights.enabled), populated at render time like UI
  // above. Gates Cards.aiSummary only -- pulse/signals/comparisons/etc. are
  // deterministic and always render. Defaults true so fixtures that predate
  // this field (e.g. mock-insights.json, used by the dev-only Teams/Components
  // views) keep showing their AI Note as before; only an explicit `false`
  // (real pipeline output with the flag off) hides it.
  var AI_ENABLED = true;

  // Icon registry: name -> inline SVG (CSP-safe, no external assets). Sport
  // config chooses which named icon each category uses -- this is only the glyph
  // library, it carries no labels and no logic. A future sport adds glyphs here
  // and references them by name from its own config. Unknown names fall back to a
  // neutral dot, so an unconfigured icon never breaks the strip.
  var ICONS = {
    mound: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2 15h16M4 15c1.5-3 4-5 6-5s4.5 2 6 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    relief: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 8a5 5 0 0 1 9-2M15 12a5 5 0 0 1-9 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M14 3v3h-3M6 17v-3h3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    bat: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 16l9-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="14.5" cy="5.5" r="2" fill="currentColor"/></svg>',
    // EPL. A boot, a shield and a rising bar -- attack, defence, form. Keyed
    // by what the category MEANS, not by the sport, so a second soccer league
    // reuses them from its own config without new artwork.
    boot: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 6h5l3 4h4a2 2 0 0 1 2 2v2H3z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M3 11h5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    shield: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3l6 2v5c0 3.5-2.5 6-6 7-3.5-1-6-3.5-6-7V5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    streak: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 15h3v-4H3zM8.5 15h3V8h-3zM14 15h3V4h-3z" fill="currentColor"/></svg>',
    _default: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3" fill="currentColor"/></svg>',
  };
  function icon(name) { return ICONS[name] || ICONS._default; }

  // The single neutral accent (used for totals / non-team-specific markets and
  // any element with no one clear associated team).
  var GOLD = "#f0a83a";

  // Max pixel height for a Recent Form bar, matching app.js's own
  // renderCategoryDetail bar math exactly, so the two vertical bar charts
  // (Who's Hot player detail, Players tab) read as one visual language.
  var BAR_MAX_PX = 54;

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
    // Drop a degenerate range: "(3–3)" says nothing "Est. 3" hasn't already
    // said. Same guard Cards.estTotal applies to the Run Estimate card, so a
    // collapsed band reads the same in both places.
    var band = o.low != null && o.high != null && o.low !== o.high
      ? " (" + o.low + "–" + o.high + ")" : "";
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
        '<div class="pulse pulse-' + pulseBand(p) + '">' +
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

    // Recent Form -- a VERTICAL bar chart, matching Who's Hot's per-category
    // detail (app.js's renderCategoryDetail): pixel-height bars scaled to the
    // series max, a value label above each bar, and an optional IP sub-label
    // below (pitcher series only -- `hasIp` mirrors app.js's own guard).
    // Series longer than 12 points drop the per-bar label (same dense-mode
    // threshold as app.js) since a 20-point window can't fit a legible label
    // over every bar in a phone-width row.
    //
    // Renders the .bars-section wrapper itself (not just the .bars-row), so
    // `title` takes the place of app.js's `barsTitle` as the .bars-label text.
    // These classes come from app.css, loaded globally on every route -- no
    // insights.css rules exist (or should be added) for the bars themselves.
    // Generic over any [{value, date, raw, ip}] series; empty when there's no
    // series.
    formBars: function (series, color, title) {
      if (!series || !series.length) return "";
      var vals = series.map(function (s) { return Number(s && s.value) || 0; });
      var maxVal = Math.max(1, Math.max.apply(null, vals)); // guard divide-by-zero
      var fill = color || GOLD;
      var hasIp = series.some(function (s) { return s && s.ip != null; });
      var dense = series.length > 12;
      var bars = series
        .map(function (s) {
          var v = Number(s && s.value) || 0;
          var hPx = Math.max(4, Math.round((v / maxVal) * BAR_MAX_PX) || 0);
          var o = v === 0 ? 0.22 : 1;
          // threshold/streak series carry the raw per-game count under `raw`
          // (met/miss drives the bar height); everything else labels with the
          // plain value, same as app.js.
          var label = s && s.raw != null ? String(Number(s.raw)) : String(v);
          var barLabel = dense ? "" : '<span class="bar-label">' + esc(label) + "</span>";
          var ipLabel = hasIp ? '<span class="bar-sublabel">' + (s && s.ip != null ? esc(s.ip) : "") + "</span>" : "";
          return (
            '<div class="bar-col">' +
            barLabel +
            '<div class="bar" style="height:' + hPx + "px;opacity:" + o + ";background:" + esc(fill) + ';"></div>' +
            ipLabel +
            "</div>"
          );
        })
        .join("");
      return (
        '<div class="bars-section"><div class="bars-label">' + esc(title || "") + "</div>" +
        '<div class="bars-row' + (hasIp ? " bars-row-ip" : "") + (dense ? " bars-row-dense" : "") + '">' + bars + "</div>" +
        "</div>"
      );
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
    //
    // COLLAPSED BY DEFAULT. Everything else in an expanded card is scannable --
    // a score, a band, a labelled number -- and costs a glance. This is the one
    // element that costs reading, and it is also the tallest, so it is the one
    // thing worth a second tap. Best Angle and the ranked rows deliberately
    // stay ungated.
    //
    // THE `clamp` / "Read full note" PAIR THAT USED TO LIVE HERE IS GONE. It
    // was already a disclosure: `story` rendered 3-line-clamped with a button
    // to un-clamp it. Keeping both would have meant tap "AI Note" -> read three
    // clipped lines -> tap again, two gates on one paragraph. One gate, and
    // past it the note is whole.
    //
    // This is a SEPARATE state from the row-level .gr-item.is-open accordion --
    // hence `is-revealed` rather than a second `is-open`. The two nest (a note
    // lives inside an open row) and must stay independently readable.
    aiSummary: function (summary, story, note) {
      if (!AI_ENABLED) return "";
      var hasNote = note && note.text;
      if (!summary && !story && !hasNote) return "";
      return (
        '<div class="ai-summary">' +
        // A real <button>, so Enter/Space and focus come from the platform
        // rather than being reimplemented. Its accessible name is the badge
        // text; the chevron is decorative and hidden from the tree.
        '<button class="ai-summary-head" type="button" data-ainote aria-expanded="false">' +
        '<span class="ai-badge">AI Note</span>' +
        '<span class="ai-chevron" aria-hidden="true">&rsaquo;</span>' +
        "</button>" +
        '<div class="ai-body"><div class="ai-body-inner">' +
        (summary ? '<p class="ai-summary-text">' + esc(summary) + "</p>" : "") +
        (story ? '<p class="ai-summary-story">' + esc(story) + "</p>" : "") +
        (hasNote ? '<div class="ai-note"><span class="ai-note-label">' + esc(note.label) + "</span>" + esc(note.text) + "</div>" : "") +
        '<div class="ai-caveat">Context, not a prediction.</div>' +
        "</div></div>" +
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
        pulse = '<span class="gr-pulse gr-pulse-' + pulseBand(g.pulse) + '">' + ps + "</span>";
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
        section("How This Result Splits",
                Cards.outcomeSplit(g.outcome_split, away, home,
                                   (g.best_angle || {}).side)) +
        section("Signal Scores", Cards.signalScores(g.signal_scores, g.best_angle, away, home)) +
        section((g.compare && g.compare.title) || "Comparison", Cards.compareMetrics(g.compare)) +
        section((g.est_total && g.est_total.label) || "Estimate", Cards.estTotal(g.est_total)) +
        section((g.f5_total && g.f5_total.label) || "First Five Estimate", Cards.estTotal(g.f5_total)) +
        block(Cards.aiSummary(g.summary, g.story, g.betting_note ? { label: "Betting signal", text: g.betting_note } : null)) +
        "</article>"
      );
    },

    // Three-way outcome split -- the measured chance of each result for a pick
    // carrying this Signal Score.
    //
    // EXISTS BECAUSE SOME SPORTS HAVE A THIRD RESULT. In MLB, NFL and CFB a
    // game has a winner and the pick either has it or does not, so `side` and
    // `other` say everything and no sport emits this. Soccer draws more than
    // one match in five, and a card that showed only the pick would hide the
    // likeliest single way it loses -- for a Double Chance pick the draw is
    // even part of what WINS it. Absent field -> renders nothing, so this costs
    // the other sports a null check and no markup.
    //
    // The bar is the three shares end to end, so their relative size is read
    // rather than computed. Draw is the neutral gold every non-team element on
    // this card already uses; side and other take the two clubs' colours, so a
    // reader maps the bar to the matchup without a legend.
    outcomeSplit: function (o, away, home, pickSide) {
      if (!o || o.side == null) return "";
      // Which club the pick is on. `side` is "ARS" or "ARS or Draw", so the
      // leading token identifies it -- the same rule sideColor() uses.
      var tok = String(pickSide || "").split(" ")[0];
      var picked = home && home.abbr === tok ? home : (away && away.abbr === tok ? away : null);
      var otherTeam = picked === home ? away : (picked === away ? home : null);
      var pickColor = (picked && picked.color) || GOLD;
      var otherColor = (otherTeam && otherTeam.color) || "rgba(255,255,255,0.35)";
      var parts = [
        { label: (picked && picked.abbr) || "Pick", pct: o.side, color: pickColor },
        { label: "Draw", pct: o.draw, color: GOLD },
        { label: (otherTeam && otherTeam.abbr) || "Other", pct: o.other, color: otherColor },
      ];
      var bar = parts.map(function (p) {
        return '<span class="os-seg" style="width:' + (100 * (p.pct || 0)).toFixed(1) +
          "%;background:" + p.color + '"></span>';
      }).join("");
      var keys = parts.map(function (p) {
        return '<div class="os-key"><span class="os-dot" style="background:' + p.color +
          '"></span>' + esc(p.label) + '<span class="os-pct">' +
          Math.round(100 * (p.pct || 0)) + "%</span></div>";
      }).join("");
      return '<div class="outcome-split"><div class="os-bar">' + bar + "</div>" +
        '<div class="os-keys">' + keys + "</div></div>";
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

    // Player Row -- the compact COLLAPSED-state row for the players list:
    // team + name + position, the player's recommended category, and a small
    // Pulse number. Mirrors Cards.gameRow -- deliberately much lighter than
    // playerInsight (no gauge, no signals, no AI text); the two are siblings,
    // not variants, and playerInsight stays the expanded view.
    //
    // Sub-elements use a "pi-row-" prefix, not "pi-": playerInsight below
    // already owns .pi-head/.pi-name/.pi-sub/.pi-team for the EXPANDED card's
    // identity line, and this is a different row with different markup, so it
    // needs its own names rather than colliding with those.
    playerRow: function (p) {
      if (!p) return "";
      var team = { abbr: p.team, color: p.team_color };
      // Compact Pulse: the number alone, same convention as Cards.gameRow's
      // .gr-pulse -- reuses pulseBand() rather than Cards.pulseScore(), since
      // the gauge belongs to the expanded card.
      var pulse = "";
      if (p.pulse && p.pulse.score != null) {
        var ps = Math.max(0, Math.min(100, Number(p.pulse.score) || 0));
        pulse = '<span class="pi-row-pulse pi-row-pulse-' + pulseBand(p.pulse) + '">' + ps + "</span>";
      }
      return (
        '<div class="pi-row">' +
        '<div class="pi-row-id">' +
        teamTag(team) +
        '<span class="pi-row-name">' + esc(p.name) + "</span>" +
        (p.pos ? '<span class="pi-row-pos">' + esc(p.pos) + "</span>" : "") +
        "</div>" +
        pulse +
        (p.series_label ? '<div class="pi-row-cat">' + esc(p.series_label) + "</div>" : "") +
        "</div>"
      );
    },

    // Player Insight -- player identity + the three sub-cards.
    playerInsight: function (p) {
      if (!p) return "";
      var color = p.team_color || GOLD;
      // POSITION FIRST, then the team chip: "RF · NYY". The two used to be the
      // other way round. Position is the weaker identifier -- it narrows what
      // kind of player this is, where the team says who they play for -- so it
      // reads as a qualifier leading into the identity, not a label after it.
      // It also puts the one team-coloured element in the sub-line at the END,
      // where it lines up with nothing else competing for the eye.
      //
      // Ordering only. Same two fields, same markup, same classes; `pos` is
      // rendered in exactly one place in this file and appears on no other
      // entity type, so there is no sibling screen to fall out of step with.
      var sub = (p.pos ? esc(p.pos) + " &middot; " : "") + '<span class="pi-team">' + esc(p.team || "") + "</span>";
      return (
        '<article class="insight-card" style="--pi-color:' + esc(color) + '">' +
        '<div class="ic-head pi-head"><div class="pi-name">' + esc(p.name) + '</div><div class="pi-sub">' + sub + "</div></div>" +
        (p.headline ? '<p class="insight-headline">' + esc(p.headline) + "</p>" : "") +
        Cards.pulseScore(p.pulse) +
        section("Key Signals", Cards.keySignals(p.signals)) +
        Cards.formBars(p.series, p.team_color, formEyebrow(p)) +
        Cards.vsStarter(p.vs_next_starter) +
        block(Cards.aiSummary(p.summary, p.story, p.matchup_note ? { label: "Matchup", text: p.matchup_note } : null)) +
        "</article>"
      );
    },
  };

  // Expose for reuse across the site.
  SP.Cards = Cards;

  // ---- page bootstrap: load mock data, render the current view ----
  var root = document.getElementById("insightsRoot");
  if (!root) return; // static pages (e.g. the hub) have no render target

  // Games list state: the rendered slate, keyed by id, so a row can build its
  // expanded card on demand without re-fetching data.json.
  var gamesById = {};

  // Players list state: same idea as gamesById, but keyed by list index --
  // players carry no stable id in data.json (unlike games' `id`), and the
  // rendered list order is fixed for the life of one render, so the index is
  // a perfectly stable key for that render's lifetime.
  var playersById = {};

  // The view currently in the DOM, set by renderView. The sport picker needs it
  // to re-render itself in place after a league change, and the delegated
  // handler that reads it is bound once for every view.
  var currentView = null;

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

  // One player in the list: the compact row plus its (initially empty) detail
  // panel. Exactly gameItem's shape, keyed by index instead of id.
  function playerItem(p, idx) {
    return (
      '<div class="pi-item" data-player-idx="' + idx + '">' +
      Cards.playerRow(p) +
      '<div class="pi-detail"><div class="pi-detail-inner"></div></div>' +
      "</div>"
    );
  }

  function closePlayerItem(item) {
    item.classList.remove("is-open");
    var r = item.querySelector(".pi-row");
    if (r) r.setAttribute("aria-expanded", "false");
  }

  // Same accordion behavior as toggleGame: opening a row closes whichever
  // player row was open, and the expanded card is built once, on first open.
  function togglePlayer(row) {
    var item = row.parentNode;
    var isOpen = item.classList.contains("is-open");
    var cur = root.querySelector(".pi-item.is-open");
    if (cur && cur !== item) closePlayerItem(cur);
    if (isOpen) return closePlayerItem(item);
    var inner = item.querySelector(".pi-detail-inner");
    if (inner && !inner.firstChild) {
      inner.innerHTML = Cards.playerInsight(playersById[item.getAttribute("data-player-idx")]);
    }
    item.classList.add("is-open");
    row.setAttribute("aria-expanded", "true");
  }

  // Light affordances (delegated, survives re-render): a game or player row
  // expands its full card; "i" reveals a hidden disclaimer sibling; the AI
  // Note header reveals its body. The row checks are first and return early
  // -- the data-toggle controls all live inside .gr-detail/.pi-detail, never
  // inside .gr-row/.pi-row, so neither contends for the same click. Games and
  // players are separate views (one or the other is ever in the DOM), but
  // both checks stay live so the same handler covers both without re-binding
  // per view.
  //
  // The AI note's state is a class on the element, exactly like .gr-item's --
  // no module-level variable to keep in sync, and it resets for free because
  // mount() re-renders the section on every visit.
  root.addEventListener("click", function (ev) {
    // The sport picker is checked FIRST and closes over its own tap: it is the
    // one control here that changes what the list contains rather than what one
    // row reveals, so nothing below should also see the click.
    var sportBtn = ev.target.closest && ev.target.closest("[data-sport]");
    if (sportBtn) {
      // Expand / choose / never mind is the shared control's decision -- see
      // sport-state.js's activate(). A key back means the league changed and is
      // already selected, so all that is left is to redraw this view under it.
      var result = SP.sport.activate(sportBtn);
      if (result !== "opened" && result !== "dismissed") mount(currentView);
      return;
    }
    // A tap anywhere else closes an open picker, then falls through so the same
    // tap still does whatever it was going to do. Mirrors app.js.
    SP.sport.close(root);
    var row = ev.target.closest && ev.target.closest(".gr-row");
    if (row) return toggleGame(row);
    var prow = ev.target.closest && ev.target.closest(".pi-row");
    if (prow) return togglePlayer(prow);
    var t = ev.target.closest && ev.target.closest("[data-toggle],[data-ainote]");
    if (!t) return;
    if (t.hasAttribute("data-ainote")) {
      var card = t.closest(".ai-summary");
      if (card) t.setAttribute("aria-expanded", card.classList.toggle("is-revealed") ? "true" : "false");
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
    if (row) {
      ev.preventDefault();
      return toggleGame(row);
    }
    var prow = ev.target.closest && ev.target.closest(".pi-row");
    if (!prow) return;
    ev.preventDefault();
    togglePlayer(prow);
  });

  // Escape closes THIS SECTION's sport picker, scoped to `root` for the same
  // reason app.js scopes its own to appEl: both containers are permanent, so
  // both pickers can be in the document at once and a document-wide query would
  // have each handler closing the wrong one. Bound on document because the key
  // can arrive while focus sits outside the section (the tab bar, or nothing at
  // all after a pointer tap).
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    var refocus = SP.sport.close(root);
    if (refocus) refocus.focus();
  });

  // ---------------- re-entry point ----------------
  // Everything above binds once, at load. mount() only fetches and re-renders,
  // so this section can be entered any number of times without re-running the
  // module or re-binding the delegated click/keydown handlers above.

  // Sources are resolved against THIS SCRIPT's URL, not the document's.
  //
  // The old "../data.json" / "mock-insights.json" were document-relative and so
  // only correct while the document itself sat in /insights/. Under the shell
  // there is one document at the site root, which would break them. Rewriting
  // them document-relative for the shell would instead break the standalone
  // pages that still ship. Root-absolute ("/data.json") would break both: this
  // deploys to a GitHub Pages *project* site, so everything is served under
  // /<repo>/, not /.
  //
  // insights.js always lives at <base>/insights/insights.js in every layout, so
  // anchoring to it resolves correctly from the shell, from the standalone
  // pages, from a local server rooted at web/, and under the Pages sub-path --
  // without the shell having to sit at the root.
  var SCRIPT_URL = (document.currentScript && document.currentScript.src) || location.href;

  // Players, games and teams all render from the live pipeline output
  // (data.json -> insights.players / insights.games / insights.teams). Only the
  // components gallery is still the deferred mock -- it is a card showcase with
  // no live equivalent, and it is not in the tab bar (direct URL only).
  function sourceFor(view) {
    var live = view === "players" || view === "games" || view === "teams";
    return new URL(live ? "../data.json" : "mock-insights.json", SCRIPT_URL).href;
  }

  // Keyed by SOURCE URL, not by view -- games, players and teams share one
  // data.json read and components is alone on the mock. So the four views cost
  // two requests, and switching between any two views backed by the same file
  // costs none. Same 10-minute ceiling as app.js, for the same reason: it outlasts
  // any tab switch but not a session resumed the next day. (The mock is a
  // committed fixture that never changes at runtime, so its entry never
  // meaningfully expires -- one rule is simpler than special-casing it.)
  //
  // Known cost: app.js keeps its own copy of data.json, so a session that
  // visits both sections fetches it twice. Sharing one cache would mean
  // reaching into loadData()'s internals, which this refactor deliberately
  // leaves untouched. One extra request per session is the price.
  var DATA_MAX_AGE_MS = 10 * 60 * 1000;
  var cache = {};

  function fetchSource(src) {
    var hit = cache[src];
    if (hit && Date.now() - hit.at < DATA_MAX_AGE_MS) return Promise.resolve(hit.data);
    return fetch(src, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("fetch " + src + " " + r.status);
        return r.json();
      })
      .then(function (data) {
        cache[src] = { data: data, at: Date.now() };
        return data;
      });
  }

  function mount(view) {
    // See the note on renderGames below: every visit starts fully collapsed.
    // That holds only because mount() always re-renders -- caching the DOM and
    // re-showing it would silently keep a row open across navigations.
    window.scrollTo(0, 0);
    return fetchSource(sourceFor(view))
      .then(function (data) { renderView(view, data, root); })
      .catch(function () {
        root.innerHTML = '<p class="empty-state">Could not load insights.</p>';
      });
  }

  // Nothing to tear down: the open row lives in the DOM as .gr-item.is-open or
  // .pi-item.is-open and goes away with the next mount()'s re-render, and
  // gamesById / playersById / UI are all reassigned on every render.
  function unmount() {}

  SP.views = SP.views || {};
  SP.views.insights = { mount: mount, unmount: unmount };

  // Without the shell (the five standalone pages that still ship today), there
  // is no router to call mount(), so self-start from the body attribute. The
  // shell marks its document with data-shell and drives mount()/unmount().
  if (!document.body.hasAttribute("data-shell")) {
    var legacyView = document.body.getAttribute("data-insights-view");
    if (legacyView) mount(legacyView);
  }

  function list(items, fn, empty) {
    if (items && items.length) return items.map(fn).join("");
    return '<p class="empty-state">' + esc(empty || "Nothing to show right now.") + "</p>";
  }

  // What a league-scoped view says when the selected league contributed nothing.
  //
  // NAMES THE LEAGUE, and deliberately claims nothing about why. Two different
  // causes land here and the payload cannot tell them apart: an off day for a
  // league that is published, and a league that has no builder feeding this view
  // at all (epl is in active_sports but not in GAME_BUILDERS, so it has
  // leaderboards and no games). "No Premier League games in this update" is true
  // either way; "no games today" would be a guess, and the wrong one half the
  // time.
  function nothingFor(label, kind) {
    return "No " + (label || "") + " " + kind + " in this update.";
  }

  function renderView(view, data, root) {
    // Remembered so the sport picker can re-render the view it is sitting in
    // without the click handler having to know which one that is.
    currentView = view;
    // Load sport-level presentation config once, before rendering any card.
    UI = (data.insights && data.insights.ui) || {};
    AI_ENABLED = data.aiInsightsEnabled !== false;
    if (view === "players") renderPlayers(data, root);
    else if (view === "games") renderGames(data, root);
    else if (view === "teams") renderTeams(data, root);
    else if (view === "components") root.innerHTML = renderGallery(data);
    else root.innerHTML = "";
  }

  // Games render as compact rows that expand in place. Every visit starts fully
  // collapsed by design: the slate turns over daily, so a remembered open row
  // would not be resuming anything -- you are re-scanning fresh games.
  //
  // Scoped to the selected league like the other two views. Today that scoping
  // can only ever empty this tab rather than re-fill it: _active_game_sports
  // resolves to [mlb], because `active_game_sports` falls back to
  // `active_sports` and is then filtered to GAME_BUILDERS, which epl is not in.
  // So a league with leaderboards but no game builder gets the named empty
  // state -- which is the honest answer, and a better one than handing it
  // another league's slate.
  function renderGames(data, root) {
    var ctx = leagueContext(data);
    var games = scoped(data.insights && data.insights.games, ctx.active);
    gamesById = {};
    games.forEach(function (g) { if (g && g.id != null) gamesById[String(g.id)] = g; });
    root.innerHTML = ctx.bar + list(games, gameItem, nothingFor(ctx.label, "games"));
    // Interactive rows, exposed to keyboard and assistive tech here rather than
    // inside Cards.gameRow -- the component stays presentational, and only the
    // wiring that actually makes rows clickable claims they are buttons.
    [].forEach.call(root.querySelectorAll(".gr-row"), function (r) {
      r.setAttribute("role", "button");
      r.setAttribute("tabindex", "0");
      r.setAttribute("aria-expanded", "false");
    });
  }

  // ---------------- league scoping ----------------
  //
  // Every live view here (players, games, teams) renders a flat array that
  // spans whatever sports the pipeline built that run, with each row carrying
  // its own `sport`. None of them filtered on it, so with mlb and epl both
  // active the Players list interleaved a Premier League goalkeeper into a
  // column of MLB hitters -- and switching to Premier League still left the
  // Games and Teams tabs showing the MLB slate. Selecting a league now means
  // the same thing on every tab: nothing from another one is on screen.

  // The leagues the picker offers, and what to call them -- sport-state.js's
  // definition, which is also the one Who's Hot uses, so the two controls
  // cannot end up offering different leagues. See SP.sport.options for why the
  // list is not simply data.sports (cfb publishes games and teams but no
  // leaderboards, so it appears in neither data.sports nor any player row).
  function sportOptions(data) {
    return SP.sport.options(data);
  }

  // Settle the selection, resolve the label, and hand back the picker markup --
  // the three things every league-scoped view needs before it can render.
  function leagueContext(data) {
    var opts = sportOptions(data);
    var active = SP.sport.ensure(opts.keys);
    var picker = SP.sport.render(opts.keys, active, opts.labels);
    return {
      active: active,
      label: opts.labels[active] || active || "",
      bar: picker ? '<div class="league-bar">' + picker + "</div>" : "",
    };
  }

  // Rows belonging to the selected league.
  //
  // AN UNTAGGED ROW IS KEPT. `sport` is what makes scoping possible, and a
  // payload without it is single-league by construction -- the committed mock
  // fixture behind the dev views is exactly that, and its games and teams carry
  // no tag at all. Dropping those would empty the view rather than scope it.
  function scoped(rows, active) {
    return (rows || []).filter(function (r) {
      return !r || !r.sport || r.sport === active;
    });
  }

  // Players render as compact rows that expand in place, mirroring renderGames
  // above. Every visit starts fully collapsed for the same reason: mount()
  // always re-renders, so a remembered open row would not be resuming
  // anything -- you are re-scanning the list fresh.
  //
  // Scoped to the selected league -- see the league-scoping block above. The
  // shared picker (sport-state.js: the same control, and the same selection, as
  // Who's Hot's header) sits above the list so the choice is visible and
  // changeable from here rather than only from the Who's Hot tab.
  function renderPlayers(data, root) {
    var ctx = leagueContext(data);
    var players = scoped(data.insights && data.insights.players, ctx.active);
    playersById = {};
    players.forEach(function (p, idx) { playersById[String(idx)] = p; });
    root.innerHTML = ctx.bar + list(players, playerItem, nothingFor(ctx.label, "players"));
    // Interactive rows, exposed to keyboard and assistive tech here rather than
    // inside Cards.playerRow -- the component stays presentational, and only
    // the wiring that actually makes rows clickable claims they are buttons.
    [].forEach.call(root.querySelectorAll(".pi-row"), function (r) {
      r.setAttribute("role", "button");
      r.setAttribute("tabindex", "0");
      r.setAttribute("aria-expanded", "false");
    });
  }

  // Teams reads insights.teams -- NOT the top-level data.teams the mock used.
  // renderGallery still reads that top-level shape, and still gets it, because
  // components is the one view still backed by the mock.
  //
  // Scoped like games, and empties for the same reason: teams ride along on the
  // game builders' slate, so a league with no builder has no team profiles.
  function renderTeams(data, root) {
    var ctx = leagueContext(data);
    var teams = scoped(data.insights && data.insights.teams, ctx.active);
    root.innerHTML = ctx.bar + list(teams, Cards.teamInsight, nothingFor(ctx.label, "team data"));
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
