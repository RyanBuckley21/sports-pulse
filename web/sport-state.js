(function () {
  "use strict";

  // The selected league, and the control that changes it.
  //
  // WHY THIS IS ITS OWN FILE. The picker was built inside app.js because Who's
  // Hot was the only section that had boards to switch between. The Players
  // view has the same need -- data.insights.players spans every active sport --
  // and it lives in insights.js, a module that by design never reaches into
  // app.js's internals. Two sections, one choice: leaving the state in either
  // one would make that section a dependency of the other, so it sits here,
  // below both, and neither imports the other.
  //
  // ONE SELECTION, NOT ONE PER SECTION. Switching to Premier League on Who's
  // Hot and finding MLB players on the Players tab would be the same class of
  // confusion this whole change is fixing -- a list that does not agree with
  // the league you picked. Both sections read and write the same value, so
  // whichever control you touch, the other agrees when you get there.
  //
  // IN MEMORY ONLY, deliberately. It matches what the choice already did: a
  // reload has always landed on the first sport in data.json. Persisting it
  // across launches is a separate decision about what "open the app" should
  // mean, not a consequence of sharing it between two views.

  var SP = window.SP || (window.SP = {});

  var selected = null;

  function esc(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // League monograms -- the league's own short name set in a circle, not a
  // picture of the sport it plays.
  //
  // THE PICTORIAL ICONS THIS REPLACED COULD NOT SCALE, and the reason is
  // structural rather than a matter of drawing them better. A glyph keyed to
  // the SPORT collapses every league that plays that sport into one shape:
  // nfl and cfb are both gridiron, so both drew the same football and became
  // indistinguishable at a glance -- two buttons the user cannot tell apart is
  // not a switcher. Monograms key to the LEAGUE, so any number of leagues
  // sharing a sport stay separable, and the picker stops needing new artwork
  // every time a sport is registered.
  //
  // The text comes from the SPORT KEY, not from the display label. The keys are
  // already the abbreviations everyone uses (mlb, nfl, cfb, epl) and they are
  // the identifier the rest of the pipeline is keyed on, so the badge cannot
  // drift from what config.yaml and SPORT_FETCHERS call the sport. The label is
  // NOT usable for this: generate_stats.SPORT_LABELS maps epl to "Premier
  // League", which is the right thing to read aloud and the wrong thing to set
  // in a 44px circle. So the label still carries the accessible name and the
  // tooltip; only the visible badge is the key.
  var MONOGRAMS = {
    // Keys that are not already their own abbreviation. "WORLDCUP" does not
    // fit and "WOR" is not a name anybody uses.
    worldcup: "WC",
  };

  function monogram(key) {
    if (MONOGRAMS[key]) return MONOGRAMS[key];
    var k = String(key || "").toUpperCase();
    // Four characters is what fits legibly at this diameter; a longer
    // unregistered key is truncated rather than allowed to overflow its circle.
    return k.length <= 4 ? k : k.slice(0, 3);
  }

  // The sport switcher: a single icon that opens into one icon per sport. It
  // replaced a two-up row of full-width pills, which spent a whole row of
  // vertical rhythm on a control that is touched rarely and has exactly one
  // bit of state.
  //
  // ONE CONTAINER, N BUTTONS -- there is no separate trigger element. Collapsed,
  // every inactive option is width:0/opacity:0 and the active one IS the
  // disclosure; expanded, they all have width. That is what lets the whole
  // thing animate as one pill widening rather than a menu appearing over the
  // header, and it means the same [data-sport] buttons serve both states.
  //
  // THE ACTIVE OPTION IS RENDERED LAST, deliberately. The picker is right-
  // aligned (margin-left:auto), so a widening container grows leftward from a
  // pinned right edge. With the active option last it sits on that pinned edge
  // and never moves as the others slide out from behind it; anywhere else in
  // the order and the icon you just tapped jumps sideways as the row opens.
  //
  // NO id ON THE CONTAINER. Both section containers are permanent (the router
  // hides and empties, it never removes), so the moment a second section
  // renders a picker the document would hold two elements with the same id and
  // getElementById would silently answer for whichever came first in the DOM.
  // Every lookup is scoped to the caller's own root instead -- see close().
  //
  // `labels` maps key -> display name; a key with no entry falls back to the
  // key itself, so a caller that has no labels to hand can pass nothing.
  function render(keys, active, labels) {
    // Nothing to switch between with a single sport -- omit it entirely (it
    // reappears automatically once a second sport is in data.json).
    if (!keys || keys.length <= 1) return "";
    var names = labels || {};
    var ordered = keys.filter(function (k) { return k !== active; });
    ordered.push(active);
    var buttons = ordered
      .map(function (key) {
        var isActive = key === active;
        var label = names[key] || key;
        return (
          '<button class="sport-opt' + (isActive ? " active" : "") + '"' +
          ' data-sport="' + esc(key) + '" type="button"' +
          // Collapsed, the active button is a disclosure; expanded, it is the
          // current choice. aria-expanded lives on it either way because it is
          // the only control a screen reader can reach while collapsed.
          (isActive ? ' aria-expanded="false" aria-current="true"' : "") +
          ' aria-label="' + esc(isActive ? label + " — switch sport" : "Switch to " + label) + '"' +
          ' title="' + esc(label) + '">' +
          esc(monogram(key)) +
          "</button>"
        );
      })
      .join("");
    return '<div class="sport-picker" data-open="false">' + buttons + "</div>";
  }

  // Collapse the picker inside `root`. Returns the button that was acting as
  // the disclosure when it was open, and null when there was nothing to close
  // -- so one return value answers both "was it open?" and "what should take
  // focus back?" for a keyboard dismissal.
  function close(root) {
    var picker = (root || document).querySelector(".sport-picker");
    if (!picker || picker.dataset.open !== "true") return null;
    picker.dataset.open = "false";
    var act = picker.querySelector(".sport-opt.active");
    if (act) act.setAttribute("aria-expanded", "false");
    return act;
  }

  // What a tap on a [data-sport] button means, decided once for every section
  // that renders the control. Returns:
  //   "opened"    the collapsed control expanded; the caller does nothing else
  //   "dismissed" the active option was tapped while open -- "never mind"
  //   <sport key> a NEW league was chosen and is now selected; caller re-renders
  function activate(btn) {
    var picker = btn.closest(".sport-picker");
    // COLLAPSED: the one visible icon is a disclosure, not a re-selection --
    // opening is the only thing a tap can mean when the other options have no
    // width to be tapped. Open and stop; the selection branch below is
    // unreachable until they are actually on screen.
    if (picker && picker.dataset.open !== "true") {
      picker.dataset.open = "true";
      btn.setAttribute("aria-expanded", "true");
      return "opened";
    }
    var key = btn.getAttribute("data-sport");
    if (key === selected) {
      close(picker);
      return "dismissed";
    }
    selected = key;
    // No explicit collapse: the caller re-renders and the markup above is
    // collapsed by default.
    return key;
  }

  // The leagues a payload carries, and what to call them -- ONE definition,
  // read by every section that renders the picker.
  //
  // data.sports is not the whole list. It holds only leagues that publish
  // LEADERBOARDS, and a league can be fully active without them: cfb ships
  // games and teams and no player boards at all, deliberately (there are no
  // player props to bet). Building the picker from data.sports alone made such
  // a league UNREACHABLE -- its games and teams were in the payload, scoped and
  // ready, with no control anywhere in the app that could select it. So the
  // keys are the union of the leaderboard leagues and the ones the pipeline
  // named in insights.ui.sport_labels (generate_insights emits one entry per
  // active GAME sport, from generate_stats.SPORT_LABELS, so the app never
  // invents a name for a league the pipeline already named).
  //
  // ORDER: leaderboard leagues first, in data.json's own order (config.yaml's
  // active_sports), then the game-only ones. That keeps the default selection
  // -- keys[0], via ensure() -- exactly what it was before this existed.
  //
  // The last fallback covers a payload with NO sports block at all (the
  // committed mock behind the dev views): the rows themselves declare their
  // league, so the picker is derived from those, in first-seen order.
  function options(data) {
    var d = data || {};
    var sports = d.sports || {};
    var ins = d.insights || {};
    var named = (ins.ui && ins.ui.sport_labels) || {};
    var keys = [], labels = {};
    function add(key, label) {
      if (!key) return;
      if (keys.indexOf(key) < 0) keys.push(key);
      if (!labels[key] && label) labels[key] = label;
    }
    Object.keys(sports).forEach(function (k) { add(k, (sports[k] || {}).label); });
    Object.keys(named).forEach(function (k) { add(k, named[k]); });
    if (!keys.length) {
      [].concat(ins.players || [], ins.games || [], ins.teams || []).forEach(function (r) {
        add(r && r.sport, null);
      });
    }
    keys.forEach(function (k) { if (!labels[k]) labels[k] = k; });
    return { keys: keys, labels: labels };
  }

  SP.sport = {
    get: function () { return selected; },
    set: function (key) { selected = key; },
    // Settle on a valid selection given the leagues actually available, and
    // return it. Keeps a still-valid choice; otherwise falls to the first key,
    // which is data.json's own order (config.yaml's active_sports order).
    // Called by every renderer, so no section has to know whether it is the
    // first one to run.
    ensure: function (keys) {
      if (!selected || !keys || keys.indexOf(selected) < 0) {
        selected = (keys && keys[0]) || null;
      }
      return selected;
    },
    options: options,
    monogram: monogram,
    render: render,
    close: close,
    activate: activate,
  };
})();
