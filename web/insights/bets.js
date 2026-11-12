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
// TODAY'S BOOST TICKETS (2026-11-12). The page used to list legs and leave
// the building to the reader. It now names the tickets for the promo typed:
// the best value per dollar, and the best for bankroll growth (Kelly). See
// `suggest` below and parlay_boost.suggest, which it copies. The promo is
// remembered in this browser, and a game that has started leaves the board.
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
  //
  // THE FILTER OPENS ON NFL when the board has NFL leans (owner, 2026-11-11),
  // and on All otherwise -- a Tuesday with only college or Premier League
  // prices must not open on an empty list. null means "not chosen yet": the
  // default is settled at the first render that has a board, and after that
  // the reader's own tap is never overridden.
  var DEFAULT_SPORT = "nfl";
  var sportFilter = null;
  var lastData = null;
  var lastRoot = null;

  function legKey(l) { return l.sport + ":" + l.id + ":" + l.side; }

  // TODAY'S PROMO, typed by the reader. There is NO default promo anywhere:
  // DraftKings' terms change by day and state (the owner, 2026-11-10: "3+
  // legs, +300 minimum", sometimes +400, sometimes a per-leg floor of -200 or
  // -250, sometimes only +100 on the ticket). A blank field means that term is
  // not part of today's offer.
  //
  // REMEMBERED BETWEEN VISITS (2026-11-12), in this browser only. The terms
  // repeat far more often than they change, and retyping four numbers on a
  // phone every visit was most of the work of using this page. What comes back
  // is what THIS reader last typed -- still not a default. localStorage can
  // throw (private mode, a full quota) and can hold anything, so both ends are
  // guarded and a bad value is just a blank form.
  var PROMO_KEY = "sp.bets.promo.v1";
  var promo = { boost: "", legs: "", total: "", floor: "" };
  (function restorePromo() {
    try {
      var saved = JSON.parse(window.localStorage.getItem(PROMO_KEY) || "null");
      if (saved && typeof saved === "object") {
        Object.keys(promo).forEach(function (k) {
          if (typeof saved[k] === "string" && saved[k].length <= 12) promo[k] = saved[k];
        });
      }
    } catch (err) { /* no storage: the form starts blank */ }
  })();
  function savePromo() {
    try { window.localStorage.setItem(PROMO_KEY, JSON.stringify(promo)); } catch (err) { /* fine */ }
  }
  var PROMO_FIELDS = [
    ["boost", "Boost %", "50"],
    ["legs", "Min legs", "3"],
    ["total", "Min total odds", "+300"],
    ["floor", "Shortest leg allowed", "-200"]
  ];

  // "+300", "-200", "−200" (a typographic minus, as phones insert) or "50%".
  // Anything else is "not set" rather than zero -- a zero would silently turn
  // a typo into a term.
  function num(s) {
    var t = String(s == null ? "" : s).replace(/\u2212/g, "-").replace(/[+%\s]/g, "");
    if (t === "" || !/^-?\d+(\.\d+)?$/.test(t)) return null;
    return parseFloat(t);
  }
  function toDecimal(american) {
    if (american == null || american === 0) return null;
    return 1 + (american > 0 ? american / 100 : 100 / Math.abs(american));
  }
  function toAmerican(dec) {
    return dec >= 2 ? "+" + Math.round((dec - 1) * 100) : "\u2212" + Math.round(100 / (dec - 1));
  }
  function signed(american) {
    return (american > 0 ? "+" : "\u2212") + Math.abs(american);
  }
  function pct(x, digits) {
    var v = (100 * x).toFixed(digits == null ? 0 : digits);
    return (x > 0 ? "+" : x < 0 ? "\u2212" : "") + v.replace("-", "") + "%";
  }

  // THE BOOST LINE. This repeats parlay_boost.evaluate for the legs picked;
  // that module holds the formula's derivation and the evidence behind the
  // "market chance" (the book's two-sided price with the vig removed -- NOT a
  // model estimate), and tools/verify/run.js pins these numbers to it.
  //   P = product of the legs' fair chances, D = product of their prices
  //   unboosted value  P*D - 1                       (what the vig costs)
  //   boosted value    P*(1 + (D-1)*(1+b)) - 1       (b multiplies WINNINGS)
  //   break-even boost (1 - P*D) / (P*D - P)
  function boostLines(legs, dec) {
    var lines = [];
    var reasons = [];
    var minLegs = num(promo.legs), minTotal = toDecimal(num(promo.total)),
        floor = num(promo.floor), floorDec = toDecimal(floor), boost = num(promo.boost);
    if (minLegs != null && legs.length < minLegs) reasons.push("needs " + minLegs + "+ legs");
    if (floorDec != null) legs.forEach(function (l) {
      if (l.decimal < floorDec - 1e-9) reasons.push(l.side + " is shorter than " + signed(floor));
    });
    if (minTotal != null && dec < minTotal - 1e-9) reasons.push("pays under " + signed(num(promo.total)));
    if (reasons.length) {
      lines.push('<div class="bet-tray-promo is-out">Doesn\u2019t qualify: ' + esc(reasons.join("; ")) + "</div>");
    } else if (minLegs != null || floorDec != null || minTotal != null) {
      lines.push('<div class="bet-tray-promo is-in">Meets the promo terms entered.</div>');
    }
    if (!legs.every(function (l) { return typeof l.fair === "number"; })) {
      lines.push('<div class="bet-tray-ev">No market chance: a leg has no two-sided price.</div>');
      return lines.join("");
    }
    var win = legs.reduce(function (p, l) { return p * l.fair; }, 1);
    var gross = win * dec;
    var need = gross > win ? (1 - gross) / (gross - win) : null;
    lines.push('<div class="bet-tray-ev">Market chance ' + (100 * win).toFixed(1) + "% \u00b7 unboosted " +
      pct(gross - 1, 1) + " per $1" +
      (need == null ? "" : need > 0 ? " \u00b7 breaks even at a " + Math.ceil(100 * need) + "% boost"
                                    : " \u00b7 already worth its stake") + "</div>");
    if (boost != null && boost >= 0) {
      var boosted = 1 + (dec - 1) * (1 + boost / 100);
      var ev = win * boosted - 1;
      lines.push('<div class="bet-tray-ev bet-tray-boosted ' + (ev > 0 ? "is-pos" : "is-neg") + '">With a ' +
        boost + "% boost: pays " + toAmerican(boosted) + ", worth " + pct(ev, 1) + " per $1</div>");
    }
    lines.push('<div class="bet-tray-fine">Chance is the book\u2019s own price with the vig removed, ' +
      "legs treated as independent. Not a model estimate; each leg\u2019s chance is only known to " +
      "about \u00b13.5 points.</div>");
    return lines.join("");
  }

  function promoForm() {
    return (
      '<div class="bet-promo" role="group" aria-label="Today\u2019s boost terms">' +
      PROMO_FIELDS.map(function (f) {
        return '<label class="bet-promo-field"><span>' + esc(f[1]) + "</span>" +
          '<input type="text" inputmode="text" autocomplete="off" class="bet-promo-input" data-promo="' +
          f[0] + '" placeholder="' + esc(f[2]) + '" value="' + esc(promo[f[0]]) + '"></label>';
      }).join("") +
      "</div>"
    );
  }

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
      // The market's own chance for this side (the price with the vig taken
      // out). It sits beside "needs" because the gap between them is the vig
      // -- the cost of the bet before any opinion about the game.
      (typeof l.fair === "number" ? "<span>market " + Math.round(100 * l.fair) + "%</span>" : "") +
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
    var shown = legs.filter(function (l) { return live(l) && (sportFilter === "all" || l.sport === sportFilter); });
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

  // A GAME THAT HAS STARTED IS NOT A BET. The page is static between deploys,
  // so the board's rows outlive their kickoffs: at 11:44 PM on 2026-11-11 two
  // 7:30 PM games were still listed. Each row now carries its start as an
  // instant (start_utc) and is dropped once the reader's own clock passes it.
  // A row with no instant (an older payload, a builder with no time) is kept:
  // hiding what cannot be dated would empty the board on a missing field.
  function live(l) {
    if (!l.start_utc) return true;
    var t = Date.parse(l.start_utc);
    return isNaN(t) || t > Date.now();
  }
  function shownLegs(board) {
    return board.straight.concat(board.parlay).filter(function (l) {
      return live(l) && (sportFilter === "all" || l.sport === sportFilter);
    });
  }

  // WHICH TICKET. A copy of parlay_boost.suggest, which holds the reasoning
  // and the measurements; tools/verify/run.js pins this copy to that module's
  // output on the same board. In short: a boost multiplies WINNINGS, so it is
  // worth most on the fewest legs the terms allow at the longest prices they
  // allow, and every extra leg costs another slice of vig. Two tickets are
  // named because "best" depends on what limits the stake:
  //   value   the highest boosted value per $1 (best when the promo caps the
  //           stake, as boosts nearly always do)
  //   growth  the highest expected growth of a bankroll staked at Kelly,
  //           f* = EV / b, g = P*ln(1 + f*b) + (1-P)*ln(1 - f*)
  // Legs are favourites only (nothing past +100): the owner's rule for boosted
  // parlays. The chance is the market's; the model is not consulted.
  var LEG_MAX_DEC = 2.0;          // +100
  var EXTRA_LEGS = 2;
  var MAX_COMBOS = 1500000;

  function combos(n, r) {
    if (r < 0 || r > n) return 0;
    var out = 1;
    for (var i = 0; i < r; i++) out = Math.floor(out * (n - i) / (i + 1) + 0.5);
    return out;
  }

  function suggest(legs) {
    var out = { eligible: 0, qualifying: 0, skipped: [], value: null, growth: null, why: null };
    var boostPct = num(promo.boost);
    if (boostPct == null || boostPct <= 0) { out.why = "no boost entered"; return out; }
    var boost = boostPct / 100;
    var floorDec = toDecimal(num(promo.floor));
    var minTotal = toDecimal(num(promo.total));
    var minLegs = Math.max(2, Math.floor(num(promo.legs) || 2));
    var seen = {};
    var pool = legs.filter(function (l) {
      if (typeof l.fair !== "number" || typeof l.decimal !== "number") return false;
      if (l.decimal > LEG_MAX_DEC + 1e-9) return false;
      if (floorDec != null && l.decimal < floorDec - 1e-9) return false;
      var g = l.sport + ":" + l.id;
      if (seen[g]) return false;
      seen[g] = true;
      return true;
    });
    pool.sort(function (a, b) {
      return b.decimal - a.decimal || (a.side + " " + a.display < b.side + " " + b.display ? -1 : 1);
    });
    out.eligible = pool.length;
    if (pool.length < minLegs) { out.why = "fewer than " + minLegs + " legs fit these terms"; return out; }

    var bestValue = null, bestGrowth = null, chosen = [];
    function walk(start, need, dec, p) {
      if (need === 0) {
        if (minTotal != null && dec < minTotal - 1e-9) return;
        out.qualifying++;
        var net = (dec - 1) * (1 + boost);
        var ev = p * (1 + net) - 1;
        var f = 0, g = 0;
        if (net > 0 && ev > 1e-12) {
          f = Math.min(ev / net, 1 - 1e-9);
          g = p * Math.log(1 + f * net) + (1 - p) * Math.log(1 - f);
        }
        if (!bestValue || ev > bestValue.ev + 1e-12 ||
            (Math.abs(ev - bestValue.ev) <= 1e-12 && p > bestValue.p + 1e-12)) {
          bestValue = { ev: ev, p: p, dec: dec, f: f, legs: chosen.slice() };
        }
        if (g > 0 && (!bestGrowth || g > bestGrowth.g + 1e-15 ||
            (Math.abs(g - bestGrowth.g) <= 1e-15 && p > bestGrowth.p + 1e-12))) {
          bestGrowth = { ev: ev, p: p, dec: dec, f: f, g: g, legs: chosen.slice() };
        }
        return;
      }
      for (var i = start; i <= pool.length - need; i++) {
        chosen.push(pool[i]);
        walk(i + 1, need - 1, dec * pool[i].decimal, p * pool[i].fair);
        chosen.pop();
      }
    }
    var sizes = [];
    for (var size = minLegs; size <= minLegs + EXTRA_LEGS && size <= pool.length; size++) {
      if (combos(pool.length, size) > MAX_COMBOS) { out.skipped.push(size); continue; }
      sizes.push(size);
    }
    sizes.forEach(function (n) { walk(0, n, 1, 1); });
    function ticket(c) {
      return { legs: c.legs, p: c.p, ev: c.ev, kelly: c.f, pays: toAmerican(1 + (c.dec - 1) * (1 + boost)) };
    }
    if (!bestValue) { out.why = "no combination of these legs meets the terms"; return out; }
    out.value = ticket(bestValue);
    out.growth = bestGrowth ? ticket(bestGrowth) : null;
    if (!bestGrowth) out.why = "no qualifying ticket is worth its stake at this boost";
    return out;
  }

  // The last suggestion painted, so "Use these legs" loads exactly the ticket
  // on screen rather than re-deriving one.
  var lastTickets = null;

  function ticketCard(kind, title, t) {
    return (
      '<div class="bet-ticket" data-ticket="' + kind + '">' +
      '<div class="bet-ticket-head"><span class="bet-ticket-title">' + esc(title) + "</span>" +
      '<span class="bet-ticket-pays">pays ' + esc(t.pays) + "</span></div>" +
      '<div class="bet-ticket-legs">' + t.legs.map(function (l) {
        return '<span class="bet-ticket-leg">' + esc(l.side) + " " + esc(l.display) + "</span>";
      }).join("") + "</div>" +
      '<div class="bet-ticket-facts"><span>cashes ' + (100 * t.p).toFixed(1) + "%</span>" +
      '<span class="' + (t.ev > 0 ? "is-pos" : "is-neg") + '">worth ' + pct(t.ev, 1) + " per $1</span>" +
      // Full Kelly, as a ceiling: the usual practice is a fraction of it,
      // because each leg's chance is only known to a few points.
      (t.kelly > 0 ? '<span class="bet-ticket-kelly">Kelly: at most ' + (100 * t.kelly).toFixed(1) + "% of bankroll</span>" : "") +
      '<button type="button" class="bet-ticket-use" data-ticket-use="' + kind + '">Use these legs</button></div>' +
      "</div>"
    );
  }

  function tickets(board) {
    var s = suggest(shownLegs(board));
    lastTickets = s;
    if (s.why === "no boost entered") {
      return '<div class="bet-tickets is-empty">Enter today’s boost and its terms: the best ' +
        "tickets on this board appear here.</div>";
    }
    var same = s.value && s.growth && s.value.legs.length === s.growth.legs.length &&
      s.value.legs.every(function (l, i) { return l === s.growth.legs[i]; });
    var cards = "";
    if (s.value && s.growth) {
      cards = same
        ? ticketCard("value", "Best value per $1, and best for bankroll growth", s.value)
        : ticketCard("value", "Best value per $1", s.value) + ticketCard("growth", "Best for bankroll growth", s.growth);
    }
    return (
      '<div class="bet-tickets">' +
      '<div class="bet-tickets-h">Today’s boost tickets</div>' +
      (cards || '<div class="bet-tickets-none">' + esc(s.why ? s.why.charAt(0).toUpperCase() + s.why.slice(1) + "." : "") +
        (s.value && !s.growth ? " The closest is " + esc(s.value.legs.map(function (l) {
          return l.side + " " + l.display; }).join(" · ")) + ", worth " + pct(s.value.ev, 1) + " per $1." : "") + "</div>") +
      '<div class="bet-tray-fine">From ' + s.eligible + " leg" + (s.eligible === 1 ? "" : "s") +
      " that fit: favourites only (nothing past +100), one per game. Take the first ticket when the " +
      "promo caps your stake; the second cashes more often and grows a bankroll fastest (Kelly). " +
      "Chances are the book’s price with the vig " +
      "removed, not the model’s, and each is only known to about ±3.5 points." +
      (s.skipped.length ? " Tickets of " + s.skipped.join(" and ") + " legs were not searched (too many)." : "") +
      "</div></div>"
    );
  }

  // THE TRAY. Combined decimal odds are the product of the legs'; the win rate
  // that price needs is its reciprocal, which is the product of the legs'
  // break-evens -- so a four-leg parlay of -200s needs 0.667^4 = 20%, and
  // shows it. "From records" appears only when every leg's price band is
  // proven; otherwise it says why it cannot.
  function tray(board) {
    var legs = board.straight.concat(board.parlay).filter(function (l) { return live(l) && picked[legKey(l)]; });
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
      boostLines(legs, dec) +
      "</div>"
    );
  }

  function filters(board) {
    var sports = [];
    board.straight.concat(board.parlay).filter(live).forEach(function (l) {
      if (sports.indexOf(l.sport) < 0) sports.push(l.sport);
    });
    if (sportFilter === null || (sportFilter !== "all" && sports.indexOf(sportFilter) < 0)) {
      // Also re-settles a choice the board no longer has (the slate turned
      // over while the page stayed open), instead of showing nothing.
      sportFilter = sports.indexOf(DEFAULT_SPORT) >= 0 ? DEFAULT_SPORT : "all";
    }
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
    var chips = filters(board);     // settles sportFilter before anything reads it
    var all = board.straight.concat(board.parlay);
    var started = all.filter(function (l) { return !live(l); }).length;
    // THE OWNER'S SINGLES LINE (-150 or longer, 2026-11-10) is narrower than
    // the list's own -200 split, so the list says how many rows meet it. It
    // also says what was measured there, because a list headed "Straight
    // bets" reads as a recommendation and the evidence does not make one:
    // the model's NFL picks at -150 or longer returned -7.4% over 279 at
    // closing prices (nfl_odds_backtest.py).
    var singles = board.singles_min;
    var singleCount = board.straight.filter(function (l) {
      return live(l) && (sportFilter === "all" || l.sport === sportFilter) &&
        singles != null && l.american >= singles;
    }).length;
    var straightNote = "Priced longer than " + line + "." + (singles == null ? "" :
      " " + singleCount + " at your singles line (" + singles + " or longer). No measured edge there: " +
      "the model has not beaten the price.");
    root.innerHTML =
      '<div class="bets">' +
      '<p class="bet-intro">Leans with a price. <b>Needs</b> is the win rate the price requires, ' +
      "<b>market</b> is the book’s own chance with the vig removed, and the <b>record</b> is how the " +
      "model’s graded picks at similar prices did (it counts from " + min + " picks). A high score is " +
      "not value: the value on this page is the boost.</p>" +
      chips +
      promoForm() +
      tickets(board) +
      tray(board) +
      section("Parlay pieces", "Priced " + line + " or shorter.", board.parlay, min) +
      section("Straight bets", straightNote, board.straight, min) +
      (started ? '<p class="bet-note bet-started">' + started + " game" + (started === 1 ? " has" : "s have") +
        " started and " + (started === 1 ? "is" : "are") + " off the board.</p>" : "") +
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
    var use = e.target.closest(".bet-ticket-use");
    if (use) {
      // Load the ticket on screen into the tray, replacing whatever was
      // picked: the tray then shows its sums and the rows show their ticks.
      var t = lastTickets && lastTickets[use.getAttribute("data-ticket-use")];
      if (!t) return;
      picked = {};
      t.legs.forEach(function (l) { picked[legKey(l)] = true; });
    } else if (f) sportFilter = f.getAttribute("data-bet-sport");
    else if (add) {
      var key = add.closest(".bet").getAttribute("data-key");
      if (picked[key]) delete picked[key]; else picked[key] = true;
    } else if (clear) picked = {};
    else return;
    render(lastData, lastRoot);
  }
  document.addEventListener("click", onClick);

  // Promo fields commit on `change` (blur or Enter), not on every keystroke,
  // and a change repaints ONLY THE TRAY, never the whole view. Both halves
  // were found by the browser suite on 2026-11-10:
  //   * typing a boost and then tapping "+" on a row blurs the field, which
  //     fires `change` between the tap's mousedown and its click. Rebuilding
  //     the rows there replaced the button under the finger and the tap was
  //     silently lost -- the leg never joined the parlay;
  //   * rebuilding the view also removes the focused field, and that removal
  //     fires a second `change` from inside the render, which threw.
  // Swapping the tray alone touches neither the rows nor the fields.
  function onChange(e) {
    if (!lastRoot || !lastRoot.contains(e.target)) return;
    var input = e.target.closest && e.target.closest(".bet-promo-input");
    if (!input) return;
    promo[input.getAttribute("data-promo")] = input.value;
    savePromo();
    var board = lastData && lastData.insights && lastData.insights.bets;
    if (!board) return;
    // The tray and the suggested tickets both read the promo; neither holds a
    // field or a row, so swapping them is safe for the reasons above.
    [[".bet-tray", tray], [".bet-tickets", tickets]].forEach(function (pair) {
      var old = lastRoot.querySelector(pair[0]);
      if (!old) return;
      var holder = document.createElement("div");
      holder.innerHTML = pair[1](board);
      old.parentNode.replaceChild(holder.firstChild, old);
    });
  }
  document.addEventListener("change", onChange);

  SP.bets = { render: render };
})();
