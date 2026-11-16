(function () {
  "use strict";

  // How old the data on screen is, said the same way on every tab.
  //
  // WHY (2026-11-16, owner: "I need a last update time stamp on all relevant
  // pages so I know I'm playing with stale data"). Only Who's Hot said anything,
  // and only "Updated 19 hours ago": no clock time, and nothing amber until a
  // full day had passed. On Saturday 2026-11-14 GitHub's scheduler held back all
  // four morning updates until 11:26 AM ET, so the site was still Friday
  // afternoon's build. Who's Hot looked fresh, and Games and Bets said nothing
  // at all -- the Bets tab had simply dropped Friday night's NBA games, which
  // had started, and its NBA filter with them.
  //
  // THE RULE IS THE SLATE DAY, NOT AN AGE. Every board here is "today's games"
  // in Eastern time (the pipeline's slate dates are Eastern -- CLAUDE.md
  // invariant 3). Data built on an earlier Eastern day is yesterday's slate,
  // however few hours old it is, so it is flagged as soon as the day turns.
  // An age cut-off would have called Saturday's 11 AM screen fresh (19 hours).
  //   built today (ET)          "Updated 9:12 AM ET · 1 hr ago"     plain
  //   built an earlier ET day   "Not updated today · last Fri 4:35 PM ET"  amber
  //   two or more days behind   the same line                       red
  // The late-night case is deliberate: at 1 AM the screen IS yesterday's slate
  // until the morning update lands, and saying so is the point.
  //
  // One small module so Who's Hot (app.js) and the four insights tabs
  // (insights.js) cannot drift into two different rules. It reads nothing but
  // the payload's generated_at and the reader's clock.

  var SP = window.SP || (window.SP = {});
  var ZONE = "America/New_York";
  // The time travels as one unbreakable piece ("Sat 11:23 AM ET"), so a stamp
  // that wraps on a phone breaks at the " · " and never strands "ET" alone.
  var NBSP = "\u00a0";

  // Eastern calendar parts for an instant, or null when this browser's Intl
  // cannot do time zones (it then falls back to the device's own clock and
  // drops the "ET" -- a wrong zone label is worse than none).
  function eastern(date) {
    try {
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit",
        weekday: "short", hour: "numeric", minute: "2-digit", hour12: true,
      }).formatToParts(date);
      var out = {};
      parts.forEach(function (p) { out[p.type] = p.value; });
      return {
        day: out.year + "-" + out.month + "-" + out.day,
        weekday: out.weekday,
        time: out.hour + ":" + out.minute + NBSP + (out.dayPeriod || "").toUpperCase(),
        zone: NBSP + "ET",
      };
    } catch (err) {
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      var h = date.getHours();
      return {
        day: date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()),
        weekday: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][date.getDay()],
        time: ((h % 12) || 12) + ":" + pad(date.getMinutes()) + NBSP + (h < 12 ? "AM" : "PM"),
        zone: "",
      };
    }
  }

  function ago(ms) {
    var mins = Math.floor(ms / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + " hr ago";
    var days = Math.floor(hours / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  function daysBetween(a, b) {
    // Two YYYY-MM-DD strings; whole calendar days from a to b.
    return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 864e5);
  }

  // {text, cls, stale, title} for a payload's generated_at, judged at `now`
  // (a Date; defaults to the reader's clock). An unreadable stamp says so
  // rather than inventing an age.
  function info(generatedAt, now) {
    var built = generatedAt ? new Date(generatedAt) : null;
    if (!built || isNaN(built.getTime())) {
      return { text: "Update time unknown", cls: "stale-1", stale: true, title: "" };
    }
    now = now || new Date();
    var b = eastern(built), n = eastern(now);
    var behind = daysBetween(b.day, n.day);
    var when = b.time + b.zone;
    var full = b.weekday + NBSP + when;
    var title = "Data built " + full + " (" + ago(now - built) + ")";
    if (behind <= 0) {
      return { text: "Updated " + when + " · " + ago(now - built), cls: "", stale: false, title: title };
    }
    return {
      text: "Not updated today · last" + NBSP + full,
      cls: behind >= 2 ? "stale-old" : "stale-1",
      stale: true,
      title: title,
    };
  }

  // Fill one element with the stamp for generatedAt. The element keeps the
  // stamp's source in data-generated so tick() can refresh it in place.
  function paint(el, generatedAt, now) {
    if (!el) return;
    var s = info(generatedAt, now);
    el.setAttribute("data-generated", generatedAt || "");
    el.className = (el.getAttribute("data-stamp-base") || "data-stamp") + (s.cls ? " " + s.cls : "");
    el.textContent = s.text;
    el.title = s.title;
    el.hidden = false;
  }

  // Every painted stamp on the page, re-judged against the clock. Run on a
  // timer so a tab left open overnight turns amber at midnight Eastern by
  // itself; the data is re-fetched on the next tab switch past ten minutes.
  function tick(now) {
    var els = document.querySelectorAll("[data-generated]");
    for (var i = 0; i < els.length; i++) paint(els[i], els[i].getAttribute("data-generated"), now);
  }

  setInterval(function () { tick(); }, 30000);

  SP.freshness = { info: info, paint: paint, tick: tick };
})();
