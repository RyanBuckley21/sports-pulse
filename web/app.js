(function () {
  "use strict";

  var SP = window.SP || (window.SP = {});

  var state = {
    data: null,
    statBySport: {},
    view: "list", // 'list' | 'detail'
    selected: null, // {sport, cat, rank}
    listScroll: 0, // page scroll of the list, restored on return from detail
    chipScroll: 0, // chip row's horizontal scroll, restored on return
  };

  // `sport` is NOT a field of this object. The selected league is shared with
  // the Players view (see sport-state.js), so it has exactly one home, and this
  // accessor forwards to it. Defined as a property rather than swapping every
  // `state.sport` in this file for a call so the reads and writes below --
  // including the ones inside render paths that have nothing to do with the
  // picker -- keep working untouched and cannot drift from the shared value.
  Object.defineProperty(state, "sport", {
    get: function () { return SP.sport.get(); },
    set: function (key) { SP.sport.set(key); },
  });

  var appEl = document.getElementById("app");

  // Opt out of the browser's OWN scroll-restoration-on-navigation feature.
  // Default is "auto", which snapshots the current scroll offset onto a
  // history entry at the moment you navigate away from it, then tries to
  // restore that snapshot when you return. That collides directly with the
  // detail view's history footprint below: the [data-rank] handler resets
  // scroll to 0 (for the detail view) BEFORE pushing the new entry, so the
  // browser's auto-snapshot for the LIST entry being left ends up recording
  // "0" -- and going back would restore that wrong snapshot instead of
  // (or racing against) returnToList()'s own state.listScroll restore.
  // "manual" leaves scroll entirely to the app's existing state.listScroll /
  // setPageScroll bookkeeping, which is already correct and was already the
  // only mechanism in play before history entries were involved at all.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  // True while this section is the one on screen. Set by mount()/unmount();
  // read only by the freshness ticker, which must not do work for a hidden view.
  var mounted = false;

  // When data.json was last read, and how old a copy may be before re-entering
  // this section refetches it.
  //
  // 10 minutes. The publish cadence sets the ceiling: the deploy workflow runs
  // on a daily cron (plus pushes), and the app itself does not call data stale
  // until 24h (freshnessClass above), so anything under an hour is already far
  // more eager than the data changes. The floor is what this guards against --
  // a standalone home-screen app stays resident for hours, so a session resumed
  // the next day must not sit on yesterday's payload. 10 minutes clears both:
  // no tab switch takes that long, so ordinary navigation never refetches and
  // the "persist" decision holds in practice, while a resumed session refreshes
  // within ten minutes of coming back. It also bounds the cost -- however many
  // times the section is re-entered, at most one refetch per ten minutes.
  var DATA_MAX_AGE_MS = 10 * 60 * 1000;
  var fetchedAt = 0;

  // Max pixel height of a recent-form bar itself, independent of the
  // row's total height -- keeps a fixed amount of headroom above the
  // tallest bar for its value label, no matter the value.
  var BAR_MAX_PX = 54;

  function esc(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  function alpha(hex, suffix) {
    return hex ? hex + suffix : "rgba(255,255,255,0.12)";
  }

  function fmtValue(value, kind) {
    // Number() coercion is defense-in-depth: values are numeric by
    // construction in generate_stats.py, but nothing rendered into the
    // DOM should trust data.json enough to pass a string through raw.
    if (kind === "rate") return Number(value).toFixed(1);
    return String(Number(value));
  }

  // threshold_rate categories display "met/window" (e.g. 8/10) instead of
  // the raw rate value that drives ranking + bar widths.
  function thresholdDisplay(p) {
    return Number(p.met) + "/" + Number(p.window);
  }

  function relativeTime(iso) {
    var diffMs = Date.now() - new Date(iso).getTime();
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
    var days = Math.floor(hours / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  function freshnessClass(iso) {
    var hoursOld = (Date.now() - new Date(iso).getTime()) / 36e5;
    if (hoursOld >= 48) return "stale-old";
    if (hoursOld >= 24) return "stale-1";
    return "";
  }

  // ---------------- data access helpers ----------------

  function currentSportData() {
    return state.data.sports[state.sport];
  }

  function currentCategory() {
    var sp = currentSportData();
    var key = state.statBySport[state.sport];
    return sp.categories.find(function (c) { return c.key === key; }) || sp.categories[0];
  }

  function selectedPlayerCtx() {
    if (!state.selected) return null;
    var sp = state.data.sports[state.selected.sport];
    var cat = sp.categories.find(function (c) { return c.key === state.selected.cat; });
    if (!cat) return null;
    var player = cat.players.find(function (p) { return p.rank === state.selected.rank; });
    if (!player) return null;
    return { sportKey: state.selected.sport, sportLabel: sp.label, cat: cat, player: player, board: cat.players };
  }

  // A player's identity ACROSS boards. data.json carries no stable player id --
  // normalizer.py declares `entity_id` but no fetcher populates it and
  // generate_stats.py drops it before emitting -- so name+team is the only key
  // available. Deliberately the same composite generate_insights.py already
  // uses (`_entity_key`, "aaron judge|nyy"), lowercased and trimmed the same
  // way, so the two surfaces group players identically rather than nearly.
  function entityKey(name, teamAbbr) {
    return String(name || "").trim().toLowerCase() + "|" + String(teamAbbr || "").trim().toLowerCase();
  }

  // Every category the selected player appears in, HOTTEST FIRST.
  //
  // Ordering is by rank ascending, which is not an invented metric: it is
  // exactly the order generate_insights.py's pulse produces, since
  // `_pulse(rank) = 100 - (rank-1)*7` is strictly decreasing in rank. Sorting
  // by rank and sorting by pulse are the same sort, so this reuses the app's
  // shipped definition of "hot" without a second copy of the formula. The
  // category key breaks ties so the order cannot flap between renders.
  function playerCategoriesCtx() {
    if (!state.selected) return null;
    var sp = state.data.sports[state.selected.sport];
    if (!sp) return null;
    var from = sp.categories.find(function (c) { return c.key === state.selected.cat; });
    var seed = from && from.players.find(function (p) { return p.rank === state.selected.rank; });
    if (!seed) return null;

    var key = entityKey(seed.entity, seed.team_abbr);
    var entries = [];
    sp.categories.forEach(function (c) {
      var p = c.players.find(function (q) { return entityKey(q.entity, q.team_abbr) === key; });
      if (p) entries.push({ cat: c, player: p, board: c.players });
    });
    entries.sort(function (a, b) {
      var d = (Number(a.player.rank) || 99) - (Number(b.player.rank) || 99);
      return d || (a.cat.key < b.cat.key ? -1 : a.cat.key > b.cat.key ? 1 : 0);
    });
    return { sportKey: state.selected.sport, sportLabel: sp.label, player: seed, entries: entries };
  }

  // The heat a rank represents, 30..100. A verbatim port of _pulse() in
  // generate_insights.py:172 -- IF THAT FORMULA CHANGES, CHANGE THIS TOO. It is
  // duplicated rather than shared because data.json carries no per-category
  // pulse (generate_insights.py emits one pulse per PLAYER, from their best
  // rank, and only for the top-20 players that get AI text), while this page
  // needs one per category for every player who can be tapped.
  function rankPulse(rank) {
    return Math.max(30, Math.min(100, 100 - ((Number(rank) || 99) - 1) * 7));
  }

  // ---------------- rendering ----------------

  function render() {
    var html = '<div class="wrap">';
    if (state.view === "detail") {
      html += renderDetail();
    } else {
      // The sport switcher is no longer a row of its own -- renderHeader()
      // places it inline with the title. See renderSportPicker.
      html += renderHeader();
      html += renderChipRow();
      html += renderList();
    }
    html += "</div>";
    appEl.innerHTML = html;
    updateScrollFade();
  }

  function renderHeader() {
    var generatedAt = state.data.generated_at;
    var cls = freshnessClass(generatedAt);
    return (
      '<header class="app-header">' +
      '<div class="app-icon"><img src="assets/icon-180.png" alt="Who\'s Hot app icon"></div>' +
      "<div>" +
      '<div class="app-title">Who\'s Hot</div>' +
      '<div class="app-status">' +
      '<span class="live-dot ' + cls + '" id="liveDot"></span>' +
      '<span class="status-text ' + cls + '" id="statusText">Updated ' + esc(relativeTime(generatedAt)) + "</span>" +
      "</div>" +
      "</div>" +
      // The "Insights →" link that used to sit here is gone: Insights is a tab
      // now, so a link from one section into another duplicated the tab bar and
      // was the only cross-document navigation left in the app.
      renderSportPicker() +
      "</header>"
    );
  }

  // The sport switcher is sport-state.js's control, rendered here into the
  // header row. This section owns only the two things that are its own: WHICH
  // leagues exist (data.json's sports block) and what each is CALLED
  // (sport.label, which the Players view has no equivalent source for). The
  // markup, the monograms, the collapsed/expanded behaviour and the selection
  // itself all live in the shared module, so the same control appears on the
  // Players tab without either section reaching into the other.
  function renderSportPicker() {
    var sportKeys = Object.keys(state.data.sports);
    var labels = {};
    sportKeys.forEach(function (k) { labels[k] = state.data.sports[k].label; });
    return SP.sport.render(sportKeys, state.sport, labels);
  }

  function renderChipRow() {
    var sp = currentSportData();
    var activeCat = currentCategory();
    var chips = sp.categories
      .map(function (c) {
        var active = c.key === activeCat.key ? " active" : "";
        return (
          '<button class="chip' + active + '" data-cat="' + c.key + '" type="button">' +
          esc(c.short_label) +
          "</button>"
        );
      })
      .join("");
    return (
      '<div class="chip-row-wrap">' +
      '<nav class="chip-row" id="chipRow">' + chips + "</nav>" +
      '<div class="chip-fade" id="chipFade"></div>' +
      "</div>"
    );
  }

  function renderList() {
    var cat = currentCategory();
    var players = cat.players;
    if (!players.length) {
      // Two different empty boards, and they deserve different sentences.
      //
      // cat.no_data present means the board is configured, real, and WAITING:
      // generate_stats emitted it deliberately because its qualification floor
      // has not been cleared yet (EPL's goals_per_appearance needs three
      // appearances, and early in a season nobody has three). Saying "no
      // qualifying players" there is true but reads as a dead end, when the
      // honest answer is that it fills itself in a few matchdays. The payload
      // carries the RULE (min_games, and how deep the other boards have got);
      // the sentence is composed here so the copy can change without a
      // pipeline change.
      //
      // Without it, the board is empty for some other reason and keeps the
      // original generic line.
      var msg = "No qualifying players right now.";
      if (cat.no_data) {
        var need = Number(cat.no_data.min_games) || 0;
        var have = Number(cat.no_data.depth) || 0;
        msg = "No data available yet.";
        if (need) {
          msg += " This board needs " + need + " appearance" + (need === 1 ? "" : "s") +
                 " per player; the deepest window so far is " + have + ".";
        }
      }
      return (
        '<div class="section-title-row"><span class="section-title">' + esc(cat.label) + "</span></div>" +
        '<p class="empty-state">' + esc(msg) + "</p>"
      );
    }
    var leaderValue = players[0].value;
    var rows = players
      .map(function (p) {
        var isLeader = p.rank === 1;
        var pct = Math.max(6, Math.round((Number(p.value) / Number(leaderValue)) * 100) || 0);
        var teamColor = p.team_color || "rgba(255,255,255,0.4)";
        var heatDot = isLeader ? '<span class="heat-dot"></span>' : "";
        var logo = p.logo_path ? '<img class="row-logo" src="' + esc(p.logo_path) + '" alt="">' : "";
        var teamMark = p.team_abbr
          ? '<span class="row-team" style="color:' + teamColor + '">' + esc(p.team_abbr) + "</span>"
          : "";
        var barShadow = isLeader ? "box-shadow:0 0 10px " + alpha(p.team_color, "80") + ";" : "";
        var valDisplay = cat.kind === "threshold" ? esc(thresholdDisplay(p)) : fmtValue(p.value, cat.kind);
        return (
          '<li><button class="player-row" data-rank="' + Number(p.rank) + '" type="button">' +
          '<div class="row-top">' +
          '<span class="row-rank">' + String(Number(p.rank)).padStart(2, "0") + "</span>" +
          '<span class="row-name">' + esc(p.entity) + "</span>" +
          heatDot +
          logo +
          teamMark +
          '<span class="row-value-wrap"><span class="row-value">' + valDisplay + "</span></span>" +
          "</div>" +
          '<div class="mag-track"><div class="mag-fill" style="width:' + pct + "%;background:" + teamColor + ";" + barShadow + '"></div></div>' +
          "</button></li>"
        );
      })
      .join("");
    return (
      '<div class="section-title-row">' +
      '<span class="section-title">' + esc(cat.label) + " &middot; " + esc(cat.sub) + "</span>" +
      '<span class="section-sub">vs leader</span>' +
      "</div>" +
      '<ol class="board">' + rows + "</ol>"
    );
  }

  // The vs-next-starter block does not vary by category and never has. It comes
  // from fetchers/mlb.py's enrich_with_vs_next_starter, which caches the next
  // opposing starter PER TEAM and the career line PER (batter, pitcher) pair --
  // both keyed well above the individual stat-category record -- so every
  // hitting-category board for the same player carries an identical object.
  // Pitching boards never get one at all (that enrichment explicitly skips
  // non-hitting categories: "this is a batter-vs-pitcher stat"). A player on N
  // hitting boards showed this same block N times before this fix.
  //
  // Scans every entry rather than trusting ctx.player (the category tapped in
  // from) because a two-way player's tapped-in category could be the pitching
  // one, whose own record has no vs_next_starter even though their hitting
  // boards do. Since the content is identical wherever it is present, the
  // first truthy one found is the correct, complete answer.
  function playerVsNextStarter(ctx) {
    for (var i = 0; i < ctx.entries.length; i++) {
      if (ctx.entries[i].player.vs_next_starter) return ctx.entries[i].player.vs_next_starter;
    }
    return null;
  }

  // The player detail page. NOT a deep-dive on the one category tapped in from
  // -- that is what the leaderboard row already showed, and repeating it at 68px
  // was the page's main redundancy. It answers "what is this player hot in right
  // now", across every board they qualify on, hottest first.
  //
  // Nothing is expanded by default, INCLUDING the category tapped in from: the
  // point is that no category is privileged. The one exception is a player who
  // qualifies on exactly one board, where there is nothing to privilege and a
  // collapsed row would just be a tap between the reader and the only content.
  function renderDetail() {
    var ctx = playerCategoriesCtx();
    if (!ctx || !ctx.entries.length) {
      state.view = "list";
      return renderList();
    }
    var player = ctx.player;
    var teamColor = player.team_color || "#ffffff";
    var soloCategory = ctx.entries.length === 1;

    var teamChipLogo = player.logo_path ? '<img src="' + esc(player.logo_path) + '" alt="">' : "";
    var teamChip = player.team_abbr
      ? '<span class="team-chip" style="color:' + teamColor + ";background:" + alpha(teamColor, "26") + '">' +
        teamChipLogo + esc(player.team_abbr) + "</span>"
      : "";
    var posLine = esc(player.team) + (player.position ? " &middot; " + esc(player.position) : "");
    // The flame now means "#1 on some board", not "#1 on the board you came
    // from" -- entries are rank-sorted, so the first entry carries the best rank.
    var bestRank = Number(ctx.entries[0].player.rank) || 99;
    var heat = bestRank === 1 ? '<span class="identity-heat"></span>' : "";

    var rows = ctx.entries
      .map(function (entry, i) {
        return renderCategorySection(entry, ctx, i === 0, soloCategory);
      })
      .join("");

    return (
      '<div class="detail-back-row">' +
      '<button class="back-btn" id="backBtn" type="button" aria-label="Back">' +
      '<svg width="9" height="15" viewBox="0 0 9 15" fill="none"><path d="M7 1 1.5 7.5 7 14" stroke="rgba(255,255,255,0.7)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>' +
      "</button>" +
      // Was "{sport} · {category}". The page is no longer about one category, so
      // naming one in the breadcrumb would promise a view it does not render.
      '<span class="crumb">' + esc(ctx.sportLabel) + "</span>" +
      "</div>" +
      '<div class="identity-row">' +
      teamChip +
      "<div><div class=\"identity-name\">" + esc(player.entity) + '</div><div class="identity-sub">' + posLine + "</div></div>" +
      heat +
      "</div>" +
      '<div class="pcat-list">' + rows + "</div>" +
      // Page-level, once -- not per category. See playerVsNextStarter above.
      renderVsNextStarter(playerVsNextStarter(ctx))
    );
  }

  // One category: the compact always-visible row, plus the full detail it opens
  // into. `hottest` is the player's best-ranked category (entries are sorted),
  // and takes the same leader treatment the leaderboard's #1 row gets -- heat
  // dot and a glow on the bar -- so "best board" reads the same in both places.
  function renderCategorySection(entry, ctx, hottest, open) {
    var cat = entry.cat;
    var player = entry.player;
    var teamColor = player.team_color || "#ffffff";

    // BAR LENGTH IS HEAT (rank), NOT SHARE-OF-LEADER. On the leaderboard
    // .mag-fill means "% of the leader in this category", which works there
    // because every row shares one leader. Here every row has a DIFFERENT
    // leader, so that number is not comparable down the column and the bars
    // would not descend with the rank ordering -- which reads as a broken sort
    // rather than as two metrics. Pulse is comparable across categories, so bar
    // length and row order carry the same signal.
    //
    // Colour stays the team colour, per the Phase 3 placement rule: .mag-fill
    // is an identity slot on the leaderboard, and re-hueing it by heat band
    // here would put a semantic colour in a slot that is team-coloured one
    // screen away. Length carries heat; colour carries identity.
    var pulse = rankPulse(player.rank);
    var barShadow = hottest ? "box-shadow:0 0 10px " + alpha(player.team_color, "80") + ";" : "";
    var heatDot = hottest ? '<span class="heat-dot"></span>' : "";
    var valDisplay = cat.kind === "threshold" ? esc(thresholdDisplay(player)) : fmtValue(player.value, cat.kind);

    return (
      '<div class="pcat-item' + (open ? " is-open" : "") + '" data-pcat="' + esc(cat.key) + '">' +
      '<button class="pcat-head" type="button" data-pcat-btn aria-expanded="' + (open ? "true" : "false") + '">' +
      '<span class="pcat-top">' +
      '<span class="pcat-label">' + esc(cat.label) + "</span>" +
      heatDot +
      '<span class="pcat-rank">#' + Number(player.rank) + "</span>" +
      '<span class="pcat-value">' + valDisplay + "</span>" +
      "</span>" +
      '<span class="mag-track"><span class="mag-fill" style="width:' + pulse + "%;background:" + teamColor + ";" + barShadow + '"></span></span>' +
      "</button>" +
      // Three nested elements, not two, and the innermost one carries the
      // padding on purpose. .pcat-detail-inner is the element that clips, and a
      // clipped box cannot shrink below its own padding -- putting the gutter
      // there would leave every collapsed category permanently open by that
      // many pixels. Same trap the AI note hit; the fix is a padded child.
      '<div class="pcat-detail"><div class="pcat-detail-inner">' +
      '<div class="pcat-detail-pad">' + renderCategoryDetail(entry, ctx) + "</div>" +
      "</div></div>" +
      "</div>"
    );
  }

  // The full per-category detail -- unchanged in content from what the page
  // rendered before this became a multi-category view. The key cells moved in
  // here from the page header: rank, ranked-of and the gap to #1/#2 are all
  // per-category numbers, so with no category privileged at the top level there
  // was no single correct value for them to show.
  function renderCategoryDetail(entry, ctx) {
    var cat = entry.cat;
    var player = entry.player;
    var board = entry.board;
    var isLeader = player.rank === 1;
    var teamColor = player.team_color || "#ffffff";
    var isSoccer = ctx.sportKey === "worldcup";

    var leaderVal = board[0].value;
    var secondVal = board.length > 1 ? board[1].value : board[0].value;
    var gapRaw = isLeader ? player.value - secondVal : leaderVal - player.value;
    var gapLabel = isLeader ? "Ahead of #2" : "Behind #1";
    // threshold gaps are rate differences -- show as percentage points
    // (e.g. +10%) rather than a bare 0.10 that reads like a count.
    var gapStr = cat.kind === "threshold"
      ? (isLeader ? "+" : "−") + Math.round(Math.abs(gapRaw) * 100) + "%"
      : (isLeader ? "+" : "−") + fmtValue(Math.abs(gapRaw), cat.kind);

    var series = player.series || [];
    var seriesCount = series.length;
    var vals = series.map(function (s) { return s.value; });
    var maxVal = Math.max(1, Math.max.apply(null, vals.length ? vals : [0]));
    // Bar heights are pixel-based (not a % of the row) so the tallest bar
    // never eats into the space reserved for its label above it.
    // Pitching series (K/G) also carry innings pitched per outing, shown
    // as a secondary label under each bar. Rendered raw: "5.2" is MLB
    // thirds notation (5 2/3 IP), standard as-is per game.
    var hasIp = series.some(function (s) { return s.ip != null; });
    // A 20-game window can't fit a legible numeric label above every bar in
    // the fixed-width column, so dense series render as a label-less
    // hit/miss sparkline (the exact per-game counts live in the breakdown).
    var dense = seriesCount > 12;
    var bars = series
      .map(function (s) {
        var hPx = Math.max(4, Math.round((Number(s.value) / Number(maxVal)) * BAR_MAX_PX) || 0);
        var o = s.value === 0 ? 0.22 : 1;
        // threshold series are binary (met/miss drives the bar height), but
        // label with the raw count that game so a met bar still shows "2"
        // hits / "6" K rather than a bare 1.
        var label = s.raw != null ? String(Number(s.raw)) : fmtValue(s.value, cat.kind);
        var barLabel = dense ? "" : '<span class="bar-label">' + esc(label) + "</span>";
        // Keep an empty sublabel slot when other bars have one, so every
        // bar in the row shares the same bottom baseline.
        var ipLabel = hasIp ? '<span class="bar-sublabel">' + (s.ip != null ? esc(s.ip) : "") + "</span>" : "";
        return (
          '<div class="bar-col">' +
          barLabel +
          '<div class="bar" style="height:' + hPx + "px;opacity:" + o + ";background:" + teamColor + ';"></div>' +
          ipLabel +
          "</div>"
        );
      })
      .join("");
    var noun = isSoccer ? "match" : "game";
    var barsTitle;
    if (cat.kind === "streak") {
      barsTitle = "Hits &middot; Last " + seriesCount + " G";
    } else if (cat.kind === "threshold") {
      // K Rate windows on starts; the hitting rates on games.
      var uw = cat.sub && cat.sub.indexOf("start") !== -1 ? "starts" : "G";
      barsTitle = "Last " + seriesCount + " " + uw;
    } else {
      barsTitle = "Per " + noun + " &middot; Last " + seriesCount + " " + (isSoccer ? "matches" : "G");
    }
    var barsHtml = seriesCount
      ? '<div class="bars-row' + (hasIp ? " bars-row-ip" : "") + (dense ? " bars-row-dense" : "") + '">' + bars + "</div>"
      : '<p class="no-series-note">No per-' + noun + " data available yet.</p>";

    var breakdownRows = buildBreakdownRows(cat, player, seriesCount, vals, isSoccer);
    breakdownRows.push({ l: gapLabel, v: gapStr });
    var breakdownHtml = breakdownRows
      .map(function (r) {
        return (
          '<div class="breakdown-row"><span class="breakdown-row-label">' + esc(r.l) +
          '</span><span class="breakdown-row-value">' + esc(r.v) + "</span></div>"
        );
      })
      .join("");

    return (
      '<div class="pcat-sub">' + esc(cat.sub).toUpperCase() + "</div>" +
      '<div class="key-row">' +
      keyCell("#" + Number(player.rank), "Rank") +
      keyCell(String(player.total_qualified != null ? player.total_qualified : "-"), "Ranked") +
      keyCell(gapStr, gapLabel) +
      "</div>" +
      '<div class="bars-section"><div class="bars-label">' + barsTitle + "</div>" + barsHtml + "</div>" +
      // vs-next-starter used to render here, once per category. It never
      // varied by category (see playerVsNextStarter, up in renderDetail) --
      // moved to the page level, once, below the category list.
      '<div class="breakdown-section"><div class="breakdown-label">Breakdown</div>' + breakdownHtml + "</div>"
    );
  }

  // "2026-07-06" -> "Jul 6". Parsed by hand: new Date("2026-07-06") is UTC
  // midnight, which renders as the previous day in US timezones.
  function fmtGameDate(iso) {
    var parts = String(iso || "").split("-");
    if (parts.length !== 3) return "";
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var m = Number(parts[1]), day = Number(parts[2]);
    if (!months[m - 1] || !day) return "";
    return months[m - 1] + " " + day;
  }

  function renderVsNextStarter(vs) {
    // Null means no starter announced yet or no head-to-head history --
    // per spec, render nothing rather than an empty block.
    if (!vs) return "";
    var date = fmtGameDate(vs.game_date);
    var title = "Vs next starter &mdash; " + esc(vs.pitcher_name) + (date ? " (" + esc(date) + ")" : "");
    var line =
      Number(vs.hits) + "-" + Number(vs.ab) +
      " &middot; " + Number(vs.hr) + " HR" +
      " &middot; " + Number(vs.rbi) + " RBI" +
      (vs.avg ? " &middot; " + esc(vs.avg) + " AVG" : "");
    var caveat = Number(vs.ab) < 10
      ? '<div class="vs-starter-caveat">Small sample &middot; ' + Number(vs.ab) + " career AB</div>"
      : "";
    return (
      '<div class="vs-starter-section">' +
      '<div class="breakdown-label">' + title + "</div>" +
      '<div class="vs-starter-line">' + line + "</div>" +
      caveat +
      "</div>"
    );
  }

  function keyCell(value, label) {
    return '<div class="key-cell"><div class="key-value">' + esc(value) + '</div><div class="key-label">' + esc(label) + "</div></div>";
  }

  function buildBreakdownRows(cat, player, seriesCount, vals, isSoccer) {
    var noun = isSoccer ? "match" : "game";
    var nounPlural = isSoccer ? "Matches" : "Games";
    if (cat.kind === "threshold") {
      // vals are binary (1 = met the threshold that game, 0 = missed).
      var pct = Math.round((Number(player.value) || 0) * 100);
      // Current streak: consecutive met counting back from the latest game
      // (0 if the most recent game missed). "Games since last miss" would be
      // the same number, so the distinct second metric is longest-in-window.
      var current = 0;
      for (var i = vals.length - 1; i >= 0; i--) {
        if (vals[i]) current++;
        else break;
      }
      var longest = 0, run = 0;
      vals.forEach(function (v) {
        if (v) { run++; if (run > longest) longest = run; }
        else run = 0;
      });
      return [
        { l: "Rate", v: Number(player.met) + " of " + Number(player.window) + " (" + pct + "%)" },
        { l: "Current streak", v: current + " G" },
        { l: "Longest streak", v: longest + " G" },
      ];
    }
    if (cat.kind === "count") {
      var avg = seriesCount ? player.value / seriesCount : 0;
      var best = vals.length ? Math.max.apply(null, vals) : 0;
      var withOne = vals.filter(function (v) { return v > 0; }).length;
      return [
        { l: "Per-" + noun + " avg", v: avg.toFixed(2) },
        { l: "Best " + noun, v: String(best) },
        { l: nounPlural + " with 1+", v: withOne + " of " + seriesCount },
      ];
    }
    if (cat.kind === "rate") {
      var peak = vals.length ? Math.max.apply(null, vals) : 0;
      var low = vals.length ? Math.min.apply(null, vals) : 0;
      return [
        { l: "Average", v: fmtValue(player.value, "rate") },
        { l: "Peak " + noun, v: peak.toFixed(1) },
        { l: "Low " + noun, v: low.toFixed(1) },
      ];
    }
    // streak
    var hitsInSpan = vals.reduce(function (a, b) { return a + b; }, 0);
    var multiHit = vals.filter(function (v) { return v >= 2; }).length;
    return [
      { l: "Streak length", v: player.value + " G" },
      { l: "Hits in span", v: String(hitsInSpan) },
      { l: "Multi-hit games", v: multiHit + " of " + seriesCount },
    ];
  }

  function updateScrollFade() {
    var wrap = document.getElementById("chipRow");
    var fade = document.getElementById("chipFade");
    if (!wrap || !fade) return;
    var atEnd = wrap.scrollWidth - wrap.scrollLeft - wrap.clientWidth <= 2;
    var scrollable = wrap.scrollWidth - wrap.clientWidth > 2;
    fade.style.opacity = atEnd || !scrollable ? "0" : "1";
  }

  // ---------------- events ----------------

  // render() rebuilds appEl.innerHTML wholesale, which resets both the window
  // scroll and the chip row's horizontal scroll. These helpers let each
  // transition capture and restore whichever positions should persist.
  function pageScroll() { return window.scrollY || document.documentElement.scrollTop || 0; }
  function setPageScroll(y) { window.scrollTo(0, y); }
  function getChipScroll() { var c = document.getElementById("chipRow"); return c ? c.scrollLeft : 0; }
  function setChipScroll(x) {
    var c = document.getElementById("chipRow");
    if (c) { c.scrollLeft = x; updateScrollFade(); }
  }

  appEl.addEventListener("click", function (e) {
    var sportBtn = e.target.closest("[data-sport]");
    if (sportBtn) {
      // What the tap MEANS (expand / choose / never mind) is the shared
      // control's decision -- see sport-state.js's activate(). It has already
      // applied the selection by the time a key comes back; what is left here
      // is this section's own reaction to a league change.
      var result = SP.sport.activate(sportBtn);
      if (result !== "opened" && result !== "dismissed") {
        state.view = "list";
        state.selected = null;
        // No explicit collapse needed: render() rebuilds the picker from
        // scratch and its markup is collapsed by default.
        render();
        setPageScroll(0); // new sport = fresh context; start at the top
      }
      return;
    }
    // A tap anywhere else in the app closes an open picker, then falls through
    // so the same tap still does whatever it was going to do.
    SP.sport.close(appEl);
    var chipBtn = e.target.closest("[data-cat]");
    if (chipBtn) {
      if (chipBtn.dataset.cat !== state.statBySport[state.sport]) {
        var y = pageScroll(), x = getChipScroll();
        state.statBySport[state.sport] = chipBtn.dataset.cat;
        state.view = "list";
        state.selected = null;
        render();
        setChipScroll(x); // keep the chip row (and page) where they were
        setPageScroll(y);
      }
      return;
    }
    var rowBtn = e.target.closest("[data-rank]");
    if (rowBtn) {
      // Remember the list's scroll positions so returning lands you back where
      // you tapped, rather than at the top.
      state.listScroll = pageScroll();
      state.chipScroll = getChipScroll();
      state.selected = { sport: state.sport, cat: currentCategory().key, rank: Number(rowBtn.dataset.rank) };
      state.view = "detail";
      render();
      setPageScroll(0); // detail opens at the top
      // Give this a real history footprint -- see the swipe-to-go-back note
      // at goBack() for why. SAME url as the list (no hash change: this must
      // never fire hashchange, which the router listens for), differentiated
      // only by the state object. One push per visit: the detail DOM has no
      // [data-rank] elements, so there is no path from one detail view
      // straight into another without passing back through the list first,
      // and this branch only ever runs from the list.
      history.pushState({ whosHotDetail: true }, "", location.href);
      return;
    }
    if (e.target.closest("#backBtn")) {
      goBack();
      return;
    }
    // Category disclosure on the detail page. Deliberately keyed off
    // [data-pcat-btn] and NOT [data-rank]: the leaderboard row branch above
    // matches any [data-rank] ancestor, so reusing that attribute here would
    // make expanding a category navigate instead.
    //
    // Toggles a class in place and does NOT call render(), which rebuilds
    // appEl.innerHTML wholesale and would drop the open row (and the page
    // scroll) on every tap. Same approach as .gr-item.is-open and
    // .ai-summary.is-revealed. Safe from being re-rendered underneath: the 30s
    // freshness interval only writes to #liveDot / #statusText, which do not
    // exist in the detail view, so it returns early here.
    var pcatBtn = e.target.closest("[data-pcat-btn]");
    if (pcatBtn) {
      var item = pcatBtn.parentNode;
      var wasOpen = item.classList.contains("is-open");
      // Accordion, matching the games list: opening one closes the other, so
      // the page stays a scannable column instead of stacking full detail.
      var openItems = appEl.querySelectorAll(".pcat-item.is-open");
      for (var i = 0; i < openItems.length; i++) {
        openItems[i].classList.remove("is-open");
        var b = openItems[i].querySelector("[data-pcat-btn]");
        if (b) b.setAttribute("aria-expanded", "false");
      }
      if (!wasOpen) {
        item.classList.add("is-open");
        pcatBtn.setAttribute("aria-expanded", "true");
      }
      return;
    }
  });

  // The actual "return to list" work, shared by both paths that can trigger
  // it: the in-app back button / hand-rolled swipe (via goBack(), below) and
  // the native OS swipe-back gesture (via the popstate listener, below --
  // that gesture bypasses every click/touch handler in this file, so it has
  // no other way to reach this).
  function returnToList() {
    state.view = "list";
    state.selected = null;
    render();
    setChipScroll(state.chipScroll || 0);
    setPageScroll(state.listScroll || 0); // land back where you left the list
  }

  function goBack() {
    returnToList();  // immediate -- no visible delay waiting on history.back()
    // Pops the entry pushed when entering detail (see the [data-rank] branch
    // above), so the real history stack matches what is now on screen. Without
    // this, the stack would grow by one every visit to a player's detail page
    // and never shrink, since returnToList() alone does not touch it.
    history.back();
  }

  // The detail view's history footprint, and the reason for it: entering
  // detail used to be a pure state swap with no history entry at all, so
  // native OS edge-swipe-back had nothing of ours to pop -- it fell straight
  // through to whatever hash-route entry was on the stack before the CURRENT
  // Who's Hot visit (i.e. whichever tab was open immediately before this one),
  // landing on that tab instead of returning to this list. The [data-rank]
  // branch above now pushes one real entry per visit to close that gap; this
  // listener is what native swipe-back actually reaches, since it never goes
  // through goBack() or any click/touch handler in this file.
  //
  // Deliberately keyed off state.view, not event.state: when goBack() is what
  // triggered the pop, state.view is already "list" by the time this fires
  // (returnToList() ran synchronously, moments earlier), so the guard below
  // is false and this is a harmless no-op -- there is exactly one place that
  // ever performs the reset, whichever path triggered it.
  //
  // No hashchange fires for any of this (the pushed entry reuses the same
  // URL), so shell.js's router never re-enters over it -- deliberately: that
  // router's mount() does an unconditional scrollTo(0,0), which would clobber
  // the scroll restore in returnToList() if a same-section hashchange fired
  // here. Popstate is the only signal used, precisely to avoid that collision.
  window.addEventListener("popstate", function () {
    if (!mounted) return; // another section owns the screen right now
    if (state.view === "detail") returnToList();
  });

  // ---------------- swipe-to-go-back ----------------
  // The detail view is a state swap, not a history entry, so iOS' native
  // edge-swipe-back (which needs browser history) does nothing here -- and in
  // standalone/home-screen mode there's no edge swipe at all. This hand-rolled
  // gesture lets a rightward drag dismiss the detail page: the page follows
  // the finger and, past a distance threshold, slides off and returns to the
  // list; otherwise it snaps back. It only engages once a drag is clearly
  // horizontal + rightward, so vertical scrolling is never intercepted. The
  // detail view has no horizontally-scrollable content, so there's nothing to
  // conflict with.
  var swipe = null;

  function activeWrap() {
    return appEl.querySelector(".wrap");
  }

  function onTouchStart(e) {
    if (state.view !== "detail" || e.touches.length !== 1) {
      swipe = null;
      return;
    }
    var t = e.touches[0];
    swipe = { x0: t.clientX, y0: t.clientY, dx: 0, decided: false, active: false };
  }

  function onTouchMove(e) {
    if (!swipe) return;
    var t = e.touches[0];
    swipe.dx = t.clientX - swipe.x0;
    var dy = t.clientY - swipe.y0;
    if (!swipe.decided) {
      // Wait until the finger has moved enough to reveal intent, then decide
      // once: a rightward, horizontally-dominant drag claims the gesture;
      // anything else (vertical scroll, leftward) is left alone for good.
      if (Math.abs(swipe.dx) < 12 && Math.abs(dy) < 12) return;
      swipe.decided = true;
      swipe.active = swipe.dx > 0 && Math.abs(swipe.dx) > Math.abs(dy);
    }
    if (!swipe.active) return;
    e.preventDefault(); // own the gesture; suppress native scroll/overscroll
    var wrap = activeWrap();
    if (!wrap) return;
    var x = Math.max(0, swipe.dx);
    wrap.style.transform = "translateX(" + x + "px)";
    wrap.style.opacity = String(Math.max(0.35, 1 - x / (window.innerWidth * 1.3)));
  }

  function onTouchEnd() {
    if (!swipe) return;
    var wrap = activeWrap();
    var committed = swipe.active && swipe.dx > Math.min(90, window.innerWidth * 0.28);
    if (wrap && swipe.active) {
      if (committed) {
        wrap.style.transition = "transform 0.18s ease, opacity 0.18s ease";
        wrap.style.transform = "translateX(100%)";
        wrap.style.opacity = "0";
        window.setTimeout(goBack, 170); // render() rebuilds a fresh, untransformed .wrap
      } else {
        // Didn't travel far enough -- ease back to rest.
        wrap.style.transition = "transform 0.2s ease, opacity 0.2s ease";
        wrap.style.transform = "";
        wrap.style.opacity = "";
        window.setTimeout(function () {
          var w = activeWrap();
          if (w) w.style.transition = "";
        }, 210);
      }
    }
    swipe = null;
  }

  // Escape closes THIS SECTION's sport picker. On document rather than appEl
  // because the key can arrive while focus sits outside the app element (the
  // tab bar, or nothing at all after a pointer tap), and an expanded picker
  // should not survive that.
  //
  // Scoped to appEl, not the document: the Players view renders the same
  // control and binds its own Escape the same way, and a document-wide query
  // would have each handler closing whichever picker happened to come first in
  // the DOM. close() returns the button that was acting as the disclosure, so
  // focus goes back to the control the user opened rather than being dropped
  // at the top of the document.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var refocus = SP.sport.close(appEl);
    if (refocus) refocus.focus();
  });

  appEl.addEventListener("touchstart", onTouchStart, { passive: true });
  appEl.addEventListener("touchmove", onTouchMove, { passive: false });
  appEl.addEventListener("touchend", onTouchEnd, { passive: true });
  appEl.addEventListener("touchcancel", onTouchEnd, { passive: true });

  appEl.addEventListener(
    "scroll",
    function (e) {
      if (e.target && e.target.id === "chipRow") updateScrollFade();
    },
    true
  );
  window.addEventListener("resize", updateScrollFade);

  // Keep the relative "Updated X ago" text and freshness color live without
  // a full re-render (avoids disrupting scroll position / open detail view).
  //
  // Created once, never cleared. Under the navigation shell this script is
  // loaded exactly once and #app is never removed, so there is no second
  // interval to leak -- `mounted` just stops it doing work while another
  // section is on screen. Returning to this view re-renders anyway, which
  // recomputes the timestamp from scratch.
  setInterval(function () {
    if (!mounted || !state.data) return;
    var dot = document.getElementById("liveDot");
    var text = document.getElementById("statusText");
    if (!dot || !text) return;
    var cls = freshnessClass(state.data.generated_at);
    dot.className = "live-dot " + cls;
    text.className = "status-text " + cls;
    text.textContent = "Updated " + relativeTime(state.data.generated_at);
  }, 30000);

  function loadData() {
    return fetch("data.json", { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        state.data = data;
        fetchedAt = Date.now();
        // Settle the shared selection against the leagues this payload
        // actually carries -- keeps a still-valid choice, otherwise falls to
        // data.json's first sport. Same rule the Players view applies, from
        // the same place, so the two can never disagree about the default.
        SP.sport.ensure(Object.keys(state.data.sports));
        Object.keys(state.data.sports).forEach(function (key) {
          var cats = state.data.sports[key].categories;
          if (!state.statBySport[key] && cats.length) state.statBySport[key] = cats[0].key;
        });
        render();
      });
  }

  // ---------------- re-entry point ----------------
  // Everything above binds once, at load. mount()/unmount() only touch content
  // and state, so this section can be entered any number of times without
  // re-running the module, re-binding listeners, or duplicating the interval.

  function mount() {
    mounted = true;
    // [hidden] resets descendant scroll (the chip row) but not the document's.
    // Without this, arriving from a long list leaves the new view scrolled --
    // this is what actually makes the listScroll:0 reset below true on screen.
    window.scrollTo(0, 0);
    if (!state.data || Date.now() - fetchedAt > DATA_MAX_AGE_MS) return loadData();
    render();
    return Promise.resolve();
  }

  function unmount() {
    mounted = false;
    // Reset policy, decided per field (persisted fields are absent by design):
    //   data, sport, statBySport  PERSIST -- data avoids a refetch per switch;
    //     sport and statBySport are user selections, and statBySport exists
    //     precisely to remember the chosen category per sport.
    //   view, selected            RESET -- returning to this section lands on
    //     the leaderboard, never a stale player detail. Matches what a full
    //     page load does today.
    //   listScroll, chipScroll    RESET -- return to the top, as today. Per-tab
    //     scroll restoration is more app-like and is a deliberate follow-up,
    //     not an oversight.
    state.view = "list";
    state.selected = null;
    state.listScroll = 0;
    state.chipScroll = 0;
  }

  SP.views = SP.views || {};
  SP.views.whosHot = { mount: mount, unmount: unmount };

  // Without the shell (the five standalone pages that still ship today), there
  // is no router to call mount(), so self-start. The shell marks its document
  // with data-shell and drives mount()/unmount() itself.
  if (!document.body.hasAttribute("data-shell")) mount();
})();
