// Bets tab: every priced moneyline lean on today's boards, across sports, as
// STRAIGHT BETS and PARLAY PIECES, with a tray that prices the legs you pick.
//
// Rendered from data.insights.bets, which bet_board.py builds. Every number on
// a row is computed there -- the price, what it needs, the record at that
// price -- so this file only lays it out. The one sum done here is the parlay
// tray's, because the legs are the reader's choice: it MULTIPLIES the decimal
// odds bet_board already converted, and turns the product back into American
// odds for display. That conversion is the only odds formula in the browser.
//
// CROSS-SPORT ON PURPOSE. Every other insights view is scoped to the league
// picker; this one answers "what is worth a look today" across all of them, so
// it has its own sport filter and ignores the picker's selection.
//
// It never ranks or colours a row as "good". A Signal Score cannot see price,
// and until a price band has PROVEN_MIN_N graded picks there is nothing to say
// a pick beats its price -- the rows say so rather than implying it.
(function () {
  "use strict";
  var SP = (window.SP = window.SP || {});

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Selected legs and the sport filter live for the page session only. The
  // slate turns over daily, so a remembered parlay would be yesterday's games.
  var picked = {};
  var sportFilter = "all";
  var lastData = null;
  var lastRoot = null;

  function legKey(l) { return l.sport + ":" + l.id + ":" + l.side; }

  // The record at this price, in words. "No graded picks yet" is the honest
  // state for nearly every band until priced picks accumulate (capture began
  // 2026-09-24) -- it is shown rather than hidden so the absence of evidence
  // is on screen next to every price.
  function recordText(r, min) {
    if (!r || !r.n) return "no graded picks at this price yet";
    var w = r.hits + "–" + (r.n - r.hits) + " (" + r.rate_display + ")";
    return r.proven ? w + " at " + r.band + " prices"
                    : w + " at " + r.band + " prices · unproven, under " + min;
  }

  function row(l, min) {
    var key = legKey(l);
    var on = !!picked[key];
    var gap = l.gap == null ? "" :
      '<span class="bet-gap ' + (l.gap > 0 ? "is-pos" : "is-neg") + '">' +
      (l.gap > 0 ? "+" : "") + Math.round(l.gap * 100) + " pts vs price</span>";
    return (
      '<li class="bet' + (on ? " is-picked" : "") + '" data-key="' + esc(key) + '">' +
      '<div class="bet-main">' +
      '<div class="bet-line">' +
      '<span class="bet-sport">' + esc(l.sport_label) + "</span>" +
      '<span class="bet-side">' + esc(l.side) + "</span>" +
      '<span class="bet-opp">' + (l.at_home ? "vs " : "@ ") + esc(l.opponent) + "</span>" +
      '<span class="bet-price">' + esc(l.display) + "</span>" +
      "</div>" +
      '<div class="bet-facts">' +
      "<span>needs " + esc(l.break_even_display) + "</span>" +
      "<span>score " + esc(l.score) + "</span>" +
      (l.start ? "<span>" + esc(l.start) + "</span>" : "") +
      (l.past_cap ? '<span class="bet-cap">past −1000 cap</span>' : "") +
      "</div>" +
      '<div class="bet-record">' + esc(recordText(l.record, min)) + gap + "</div>" +
      (l.notes && l.notes.length
        ? '<div class="bet-notes">' + l.notes.map(function (n) {
            return '<div class="bet-warn">' + esc(n) + "</div>";
          }).join("") + "</div>"
        : "") +
      "</div>" +
      '<button type="button" class="bet-add" aria-pressed="' + on + '" aria-label="' +
      (on ? "Remove " : "Add ") + esc(l.side) + ' to parlay">' + (on ? "✓" : "+") + "</button>" +
      "</li>"
    );
  }

  function section(title, note, legs, min) {
    var shown = legs.filter(function (l) { return sportFilter === "all" || l.sport === sportFilter; });
    return (
      '<section class="bet-section">' +
      '<h2 class="bet-h">' + esc(title) + ' <span class="bet-count">' + shown.length + "</span></h2>" +
      '<p class="bet-note">' + esc(note) + "</p>" +
      (shown.length
        ? '<ul class="bet-list">' + shown.map(function (l) { return row(l, min); }).join("") + "</ul>"
        : '<div class="empty-state">None on this board.</div>') +
      "</section>"
    );
  }

  // THE TRAY. Combined decimal odds are the product of the legs'; the win rate
  // that price needs is its reciprocal, which is the product of the legs'
  // break-evens -- so a four-leg parlay of -200s needs 0.667^4 = 20%, and
  // shows it. "From records" appears only when every leg's price band is
  // proven; otherwise it says why it cannot.
  function tray(board) {
    var legs = board.straight.concat(board.parlay).filter(function (l) { return picked[legKey(l)]; });
    if (!legs.length) {
      return '<div class="bet-tray is-empty">Tap + on any row to build a parlay.</div>';
    }
    var dec = legs.reduce(function (p, l) { return p * l.decimal; }, 1);
    var american = dec >= 2 ? "+" + Math.round((dec - 1) * 100) : "−" + Math.round(100 / (dec - 1));
    var needs = Math.round(100 / dec) + "%";
    var proven = legs.every(function (l) { return l.record && l.record.proven; });
    var est = proven
      ? Math.round(100 * legs.reduce(function (p, l) { return p * l.record.rate; }, 1)) + "% from records"
      : "no proven record for every leg";
    return (
      '<div class="bet-tray">' +
      '<div class="bet-tray-main">' +
      '<span class="bet-tray-legs">' + legs.length + (legs.length === 1 ? " leg" : " legs") + "</span>" +
      '<span class="bet-tray-price">' + american + "</span>" +
      '<span class="bet-tray-needs">needs ' + needs + "</span>" +
      '<button type="button" class="bet-clear">Clear</button>' +
      "</div>" +
      '<div class="bet-tray-sub">' + esc(legs.map(function (l) { return l.side + " " + l.display; }).join(" · ")) +
      " — " + esc(est) + "</div>" +
      "</div>"
    );
  }

  function filters(board) {
    var sports = [];
    board.straight.concat(board.parlay).forEach(function (l) {
      if (sports.indexOf(l.sport) < 0) sports.push(l.sport);
    });
    var labels = {};
    board.straight.concat(board.parlay).forEach(function (l) { labels[l.sport] = l.sport_label; });
    var opts = [["all", "All"]].concat(sports.map(function (s) { return [s, labels[s]]; }));
    return (
      '<div class="bet-filters" role="group" aria-label="Sport">' +
      opts.map(function (o) {
        // data-bet-sport, NOT data-sport: insights.js's root click handler
        // treats any [data-sport] as the league picker, so the shared name
        // swallowed these taps and moved the global league selection instead.
        return '<button type="button" class="bet-filter" data-bet-sport="' + esc(o[0]) + '" aria-pressed="' +
          (sportFilter === o[0]) + '">' + esc(o[1]) + "</button>";
      }).join("") +
      "</div>"
    );
  }

  function render(data, root) {
    lastData = data;
    lastRoot = root;
    var board = data && data.insights && data.insights.bets;
    if (!board) {
      root.innerHTML = '<div class="empty-state">No bets board in this update.</div>';
      return;
    }
    var min = board.proven_min_n;
    var line = board.parlay_max;
    root.innerHTML =
      '<div class="bets">' +
      '<p class="bet-intro">Every priced moneyline lean today. <b>Needs</b> is the win rate the price ' +
      "requires. The <b>record</b> is how the model's graded picks at similar prices did — it only " +
      "counts once it reaches " + min + " picks. Scores don't see prices, so a high score is not value " +
      "by itself; both lists are sorted by score until a record is proven.</p>" +
      filters(board) +
      tray(board) +
      section("Parlay pieces", "Priced " + line + " or shorter.", board.parlay, min) +
      section("Straight bets", "Priced longer than " + line + ".", board.straight, min) +
      (board.unpriced ? '<p class="bet-note">' + board.unpriced +
        " more lean" + (board.unpriced === 1 ? " has" : "s have") + " no price from the book.</p>" : "") +
      "</div>";
  }

  // One delegated handler for the whole view: filter chips, the add buttons,
  // and Clear. Re-rendering from the last data is cheap and keeps the tray and
  // the rows' pressed state from ever disagreeing.
  function onClick(e) {
    if (!lastRoot || !lastRoot.contains(e.target)) return;
    var f = e.target.closest(".bet-filter");
    var add = e.target.closest(".bet-add");
    var clear = e.target.closest(".bet-clear");
    if (f) sportFilter = f.getAttribute("data-bet-sport");
    else if (add) {
      var key = add.closest(".bet").getAttribute("data-key");
      if (picked[key]) delete picked[key]; else picked[key] = true;
    } else if (clear) picked = {};
    else return;
    render(lastData, lastRoot);
  }
  document.addEventListener("click", onClick);

  SP.bets = { render: render };
})();
