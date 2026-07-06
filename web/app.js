(function () {
  "use strict";

  var state = {
    data: null,
    sport: null,
    statBySport: {},
    view: "list", // 'list' | 'detail'
    selected: null, // {sport, cat, rank}
  };

  var appEl = document.getElementById("app");

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

  // ---------------- rendering ----------------

  function render() {
    var html = '<div class="wrap">';
    if (state.view === "detail") {
      html += renderDetail();
    } else {
      html += renderHeader();
      html += renderSportToggle();
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
      '<div class="app-icon"><div class="dot"></div></div>' +
      "<div>" +
      '<div class="app-title">Who\'s Hot</div>' +
      '<div class="app-status">' +
      '<span class="live-dot ' + cls + '" id="liveDot"></span>' +
      '<span class="status-text ' + cls + '" id="statusText">Updated ' + esc(relativeTime(generatedAt)) + "</span>" +
      "</div>" +
      "</div>" +
      '<button class="refresh-btn" id="refreshBtn" aria-label="Refresh" type="button">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"></path><path d="M21 3v6h-6"></path></svg>' +
      "</button>" +
      "</header>"
    );
  }

  function renderSportToggle() {
    var sportKeys = Object.keys(state.data.sports);
    var buttons = sportKeys
      .map(function (key) {
        var active = key === state.sport ? " active" : "";
        return (
          '<button class="sport-btn' + active + '" data-sport="' + key + '" type="button">' +
          esc(state.data.sports[key].label) +
          "</button>"
        );
      })
      .join("");
    return '<nav class="sport-toggle">' + buttons + "</nav>";
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
      return (
        '<div class="section-title-row"><span class="section-title">' + esc(cat.label) + "</span></div>" +
        '<p class="empty-state">No qualifying players right now.</p>'
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
        return (
          '<li><button class="player-row" data-rank="' + Number(p.rank) + '" type="button">' +
          '<div class="row-top">' +
          '<span class="row-rank">' + String(Number(p.rank)).padStart(2, "0") + "</span>" +
          '<span class="row-name">' + esc(p.entity) + "</span>" +
          heatDot +
          logo +
          teamMark +
          '<span class="row-value-wrap"><span class="row-value">' + fmtValue(p.value, cat.kind) + "</span></span>" +
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

  function renderDetail() {
    var ctx = selectedPlayerCtx();
    if (!ctx) {
      state.view = "list";
      return renderList();
    }
    var cat = ctx.cat;
    var player = ctx.player;
    var board = ctx.board;
    var isLeader = player.rank === 1;
    var teamColor = player.team_color || "#ffffff";
    var isSoccer = ctx.sportKey === "worldcup";

    var teamChipLogo = player.logo_path ? '<img src="' + esc(player.logo_path) + '" alt="">' : "";
    var teamChip = player.team_abbr
      ? '<span class="team-chip" style="color:' + teamColor + ";background:" + alpha(teamColor, "26") + '">' +
        teamChipLogo + esc(player.team_abbr) + "</span>"
      : "";

    var posLine = esc(player.team) + (player.position ? " &middot; " + esc(player.position) : "");
    var heat = isLeader ? '<span class="identity-heat"></span>' : "";

    var leaderVal = board[0].value;
    var secondVal = board.length > 1 ? board[1].value : board[0].value;
    var gapRaw = isLeader ? player.value - secondVal : leaderVal - player.value;
    var gapLabel = isLeader ? "Ahead of #2" : "Behind #1";
    var gapStr = (isLeader ? "+" : "−") + fmtValue(Math.abs(gapRaw), cat.kind);

    var series = player.series || [];
    var seriesCount = series.length;
    var vals = series.map(function (s) { return s.value; });
    var maxVal = Math.max(1, Math.max.apply(null, vals.length ? vals : [0]));
    // Bar heights are pixel-based (not a % of the row) so the tallest bar
    // never eats into the space reserved for its label above it.
    var bars = series
      .map(function (s) {
        var hPx = Math.max(4, Math.round((Number(s.value) / Number(maxVal)) * BAR_MAX_PX) || 0);
        var o = s.value === 0 ? 0.22 : 1;
        var label = fmtValue(s.value, cat.kind);
        return (
          '<div class="bar-col">' +
          '<span class="bar-label">' + esc(label) + "</span>" +
          '<div class="bar" style="height:' + hPx + "px;opacity:" + o + ";background:" + teamColor + ';"></div>' +
          "</div>"
        );
      })
      .join("");
    var noun = isSoccer ? "match" : "game";
    var barsTitle =
      cat.kind === "streak"
        ? "Hits &middot; Last " + seriesCount + " G"
        : "Per " + noun + " &middot; Last " + seriesCount + " " + (isSoccer ? "matches" : "G");
    var barsHtml = seriesCount
      ? '<div class="bars-row">' + bars + "</div>"
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
      '<div class="detail-back-row">' +
      '<button class="back-btn" id="backBtn" type="button" aria-label="Back">' +
      '<svg width="9" height="15" viewBox="0 0 9 15" fill="none"><path d="M7 1 1.5 7.5 7 14" stroke="rgba(255,255,255,0.7)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>' +
      "</button>" +
      '<span class="crumb">' + esc(ctx.sportLabel) + " &middot; " + esc(cat.label) + "</span>" +
      "</div>" +
      '<div class="identity-row">' +
      teamChip +
      "<div><div class=\"identity-name\">" + esc(player.entity) + '</div><div class="identity-sub">' + posLine + "</div></div>" +
      heat +
      "</div>" +
      '<div class="hero-row">' +
      '<div class="hero-value" style="color:' + teamColor + '">' + fmtValue(player.value, cat.kind) + "</div>" +
      '<div class="hero-caption"><div class="hero-cat">' + esc(cat.label) + '</div><div class="hero-sub">' +
      esc(cat.sub).toUpperCase() + " &middot; #" + Number(player.rank) + "</div></div>" +
      "</div>" +
      '<div class="key-row">' +
      keyCell("#" + Number(player.rank), "Rank") +
      keyCell(String(player.total_qualified != null ? player.total_qualified : "-"), "Ranked") +
      keyCell(gapStr, gapLabel) +
      "</div>" +
      '<div class="bars-section"><div class="bars-label">' + barsTitle + "</div>" + barsHtml + "</div>" +
      '<div class="breakdown-section"><div class="breakdown-label">Breakdown</div>' + breakdownHtml + "</div>"
    );
  }

  function keyCell(value, label) {
    return '<div class="key-cell"><div class="key-value">' + esc(value) + '</div><div class="key-label">' + esc(label) + "</div></div>";
  }

  function buildBreakdownRows(cat, player, seriesCount, vals, isSoccer) {
    var noun = isSoccer ? "match" : "game";
    var nounPlural = isSoccer ? "Matches" : "Games";
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

  appEl.addEventListener("click", function (e) {
    var sportBtn = e.target.closest("[data-sport]");
    if (sportBtn) {
      var sport = sportBtn.dataset.sport;
      if (sport !== state.sport) {
        state.sport = sport;
        state.view = "list";
        state.selected = null;
      }
      render();
      return;
    }
    var chipBtn = e.target.closest("[data-cat]");
    if (chipBtn) {
      state.statBySport[state.sport] = chipBtn.dataset.cat;
      state.view = "list";
      state.selected = null;
      render();
      return;
    }
    var rowBtn = e.target.closest("[data-rank]");
    if (rowBtn) {
      state.selected = { sport: state.sport, cat: currentCategory().key, rank: Number(rowBtn.dataset.rank) };
      state.view = "detail";
      render();
      return;
    }
    if (e.target.closest("#backBtn")) {
      state.view = "list";
      render();
      return;
    }
    if (e.target.closest("#refreshBtn")) {
      refresh();
      return;
    }
  });

  appEl.addEventListener(
    "scroll",
    function (e) {
      if (e.target && e.target.id === "chipRow") updateScrollFade();
    },
    true
  );
  window.addEventListener("resize", updateScrollFade);

  function refresh() {
    var btn = document.getElementById("refreshBtn");
    if (btn) btn.classList.add("spinning");
    loadData().finally(function () {
      if (btn) btn.classList.remove("spinning");
    });
  }

  // Keep the relative "Updated X ago" text and freshness color live without
  // a full re-render (avoids disrupting scroll position / open detail view).
  setInterval(function () {
    if (!state.data) return;
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
        if (!state.sport || !state.data.sports[state.sport]) {
          state.sport = Object.keys(state.data.sports)[0];
        }
        Object.keys(state.data.sports).forEach(function (key) {
          var cats = state.data.sports[key].categories;
          if (!state.statBySport[key] && cats.length) state.statBySport[key] = cats[0].key;
        });
        render();
      });
  }

  loadData();
})();
