"""NBA's grading rules, for signal_report.py -- written now, registered later.

NOT YET IN signal_report.SPORT_ADAPTERS. An adapter entry also needs
list_markets / top_market, which belong to nba_signals.py, and that module
cannot exist before the backtest gives it weights. Registering an adapter whose
ranking raises would be worse than not registering it. The rules themselves do
not depend on any of that, so they are written and pinned now (test_nba), and
step 2 adds the two delegating functions and the SPORT_ADAPTERS entry.

WHAT IS DIFFERENT ABOUT NBA, each a rule rather than an accident:

  THE STORE KEY IS ESPN'S EVENT ID. The future builder reads the same
  scoreboard this grader reads, so no re-keying is needed (contrast
  nfl_grading, which joins ESPN ids to nflverse game_ids).

  A TIE CANNOT HAPPEN. Overtime continues until someone wins, so a level
  final score means the feed is broken: UNRESOLVED, as in cfb_grading, and
  never PUSH as in nfl_grading. A 0-0 "final" is the likeliest form it takes.

  THE SLATE IS AN EASTERN DAY. ESPN stamps events in UTC and most NBA games
  tip after 8 PM Eastern, so a date's games are split across two UTC days.
  fetch_slate therefore asks for both days' months and keeps the events whose
  EASTERN date is the one being graded -- it does not trust which day ESPN's
  own `dates=` parameter files a late game under, which was never measured.

  ONLY COUNTED GAMES ARE GRADED. All-Star games and the NBA Cup final pass a
  season-type check and would otherwise reach the ledger; preseason would too.
  fetchers/nba.is_counted_game is the one rule, used by both builder and
  grader. Playoff and play-in games are NOT handled: their competition types
  have not been captured, and the regular season ends mid-April. Capture them
  before then (open item in CLAUDE.md).

The verdict vocabulary is signal_report's (HIT / MISS / PENDING / POSTPONED /
UNRESOLVED); moneyline needs no line, so UNPRICED is unreachable.
"""

import datetime

import espn_dates
from fetchers import nba

CALLED_OFF_STATES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED",
                     "STATUS_SUSPENDED"}
_ESPN_PHASES = {1: "preseason", 2: "regular", 3: "postseason"}


def _scoreboard_url(config=None):
    """config's `nba.scoreboard_url`, else the module constant. test_nba
    asserts the two agree, as the other sports' grading tests do."""
    return ((config or {}).get("nba") or {}).get("scoreboard_url") or nba.SCOREBOARD_URL


def _status(event):
    return ((nba._comp(event).get("status") or {}).get("type")) or {}


def fetch_slate(session, config, date):
    """{event id: event} for every counted game played on Eastern date `date`
    (YYYY-MM-DD). Two days' months are walked so a game stamped with the next
    UTC day is still found -- one request normally, two at a month end."""
    day = datetime.date.fromisoformat(date)
    url = _scoreboard_url(config)
    events = espn_dates.fetch_window(
        lambda params: {"events": nba.get_events(session, params, url)},
        day, day + datetime.timedelta(days=1))
    return slate_from_events(events, date)


def slate_from_events(events, date):
    """The filter fetch_slate applies, split out so it is tested on real
    captured events with no network."""
    return {str(e.get("id")): e for e in events
            if nba.is_counted_game(e) and nba.slate_date(e) == date}


def fetch_replay_dates(session, config, pks):
    """Postponed NBA games are rescheduled weeks later as a NEW event id, so a
    replay cannot be followed by id the way MLB's can. The pick stays
    POSTPONED; returning nothing is the honest answer."""
    return {}


def season_phase(event):
    """"regular" for a counted game; "preseason" / "postseason" by season
    type; None for anything else, including an All-Star game or the Cup final,
    which carry season.type 2 and must not be read as regular season."""
    if not event:
        return None
    stype = (event.get("season") or {}).get("type")
    if stype == nba.REGULAR_SEASON_TYPE:
        return "regular" if nba.is_counted_game(event) else None
    return _ESPN_PHASES.get(stype)


def is_final(event):
    """Completed, with both scores readable. Reads `completed` rather than a
    status name, the lesson cfb_grading records: an overtime final keeps its
    name and moves the overtime into the detail text."""
    row = nba.parse_game(event) if event else None
    return bool(row) and row["home_score"] is not None and row["away_score"] is not None


def is_called_off(event):
    return _status(event).get("name") in CALLED_OFF_STATES


def live_state(event):
    st = _status(event)
    return st.get("shortDetail") or st.get("detail") or st.get("description") or "Scheduled"


def observed_facts(event):
    """Raw facts a verdict can be re-derived from, stored with every pick."""
    if event is None or not is_final(event):
        return None
    row = nba.parse_game(event)
    h, a = row["home_score"], row["away_score"]
    return {"away_score": a, "home_score": h,
            "result": "H" if h > a else ("A" if a > h else "T"),
            "neutral_site": row["neutral_site"],
            "state": _status(event).get("description")}


def grade(pick, event, assume_lines):
    """(result_text, verdict, basis) for one NBA pick against one game.
    `assume_lines` is accepted and ignored (an MLB totals concern)."""
    away, home, side = pick["away_abbr"], pick["home_abbr"], pick["side"]
    if event is None:
        return "not on this date's schedule", "UNRESOLVED", None
    if is_called_off(event):
        return _status(event).get("description") or "Postponed", "POSTPONED", None
    if not is_final(event):
        return live_state(event), "PENDING", None
    picked = str(side or "").split(" ")[0]
    if picked not in (away, home):
        return "side {!r} names neither team".format(side), "UNRESOLVED", None
    if pick["bet_type"] != "moneyline":
        return "bet type {!r} has no NBA grading rule".format(pick["bet_type"]), "UNRESOLVED", None
    facts = observed_facts(event)
    text = "{} {}-{} {}".format(away, facts["away_score"], facts["home_score"], home)
    if facts["result"] == "T":
        return text + " (level -- impossible in the NBA, so the feed is wrong)", "UNRESOLVED", None
    winner = home if facts["result"] == "H" else away
    return text, "HIT" if winner == picked else "MISS", "outcome"
