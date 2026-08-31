"""NFL's grading adapter for signal_report.py.

Same arrangement epl_grading.py and cfb_grading.py have: signal_report was
written around MLB's StatsAPI shape and no other sport answers to it, so this
supplies NFL's implementations of the small surface the grading rules need and
signal_report dispatches through SPORT_ADAPTERS. MLB's, EPL's and CFB's entries
are their own functions, unchanged and uncalled from here.

WHAT IS DIFFERENT ABOUT NFL, and why each of these is a rule rather than an
accident:

  THE STORE'S KEY IS NOT THE FEED'S KEY. Every other sport here stores games
  under an id its own grading feed also uses. fetchers/nfl keys the store by
  nflverse's `game_id` ("2026_01_DAL_PHI"), which ESPN has never heard of. But
  nflverse's games.csv carries an `espn` column holding ESPN's own event id, so
  the join exists in data already -- fetch_slate re-keys the ESPN scoreboard
  through it and hands signal_report a slate in the store's own id space.
  Verified on a real season: all 272 rows of 2026 carry an espn id.

  A TIE IS A REAL RESULT. Unlike college football, an NFL regular-season game
  can end level -- overtime does not have to produce a winner, and roughly one
  a season does. A moneyline tie is a PUSH at every book, so it grades PUSH:
  not a HIT (the named side did not win), not a MISS (the bettor was not
  wrong), and emphatically not UNRESOLVED, which is what cfb_grading returns
  for the same score line because there it means the feed is broken. Getting
  this backwards is a quiet one-a-season error in an append-only ledger.

  THE STORE SPANS A WEEK. fetchers/nfl.build_game_entities looks seven days
  ahead (the NFL week runs Thursday to Monday; a one-date slate leaves the
  Games tab empty most days), so store_spans_dates is True and a pick whose
  game is not on the graded date is DEFERRED rather than UNRESOLVED.

The verdict vocabulary is signal_report's (HIT / MISS / PUSH / PENDING /
POSTPONED / UNRESOLVED / UNPRICED) and this module returns nothing outside it.
UNPRICED is unreachable -- moneyline needs no line.
"""

import datetime

REQUEST_TIMEOUT = 30
ESPN_NFL_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
                       "football/nfl/scoreboard")
# ESPN status names meaning "no result on this date".
CALLED_OFF_STATES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED",
                     "STATUS_ABANDONED", "STATUS_SUSPENDED"}
# How far ahead to look for a postponed game that was replayed. The NFL moves a
# game by days, not months (a weather postponement is usually played Monday or
# Tuesday of the same week), so this is deliberately short -- a wide window
# would only ever find next season's fixture between the same two clubs.
REPLAY_LOOKAHEAD_DAYS = 21


def _scoreboard_url(config=None):
    """(url,) for ESPN's NFL scoreboard, from config's `nfl` block with this
    module's constant as the fallback. Mirrors cfb_grading._scoreboard_url;
    tools/verify/test_nfl_grading.py asserts the two agree so a grader cannot
    quietly read a different endpoint from the one documented."""
    cfg = (config or {}).get("nfl") or {}
    return cfg.get("scoreboard_url") or ESPN_NFL_SCOREBOARD


def _status(event):
    comp = (event.get("competitions") or [{}])[0]
    return (comp.get("status") or {}).get("type") or {}


def _sides(event):
    """{"home": {...}, "away": {...}} with abbr and int score, or {} if the
    event is not a readable two-sided game."""
    comp = (event.get("competitions") or [{}])[0]
    out = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        try:
            score = int(c.get("score"))
        except (TypeError, ValueError):
            score = None
        out[c.get("homeAway")] = {"abbr": team.get("abbreviation"),
                                  "name": team.get("displayName"), "score": score}
    return out if set(out) == {"home", "away"} else {}


def _espn_to_nflverse(session, date):
    """{espn event id: nflverse game_id} for the season `date` falls in.

    This is the join the module docstring describes. One games.csv fetch, which
    fetchers/nfl already caches nothing of -- it is a single plain CSV and the
    grader runs once a day."""
    from fetchers import nfl
    season = nfl.season_for_date(date)
    out = {}
    for row in nfl.get_schedule(session, season):
        espn_id, game_id = (row.get("espn") or "").strip(), row.get("game_id")
        if espn_id and game_id:
            out[espn_id] = game_id
    return out


def _fetch_events(session, config, params):
    url = _scoreboard_url(config)
    r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("events") or []


def fetch_slate(session, config, date):
    """{gamePk: event} for one date, keyed by NFLVERSE's game_id -- the key
    fetchers/nfl writes into the store -- not by ESPN's own event id.

    An ESPN event with no nflverse counterpart is DROPPED rather than kept
    under its raw id. That is not defensive padding: ESPN's NFL scoreboard
    carries preseason and Pro Bowl events that games.csv does not, and keeping
    one under an id the store can never match would put a permanently
    UNRESOLVED row in front of whoever reads the report.
    """
    id_map = _espn_to_nflverse(session, date)
    out = {}
    for e in _fetch_events(session, config, {"dates": date.replace("-", ""), "limit": 1000}):
        game_id = id_map.get(str(e.get("id")))
        if game_id:
            out[game_id] = e
    return out


def fetch_replay_dates(session, config, pks):
    """{gamePk: date} for a postponed game that has since been played. One
    ranged call rather than one per pk -- see REPLAY_LOOKAHEAD_DAYS."""
    if not pks:
        return {}
    today = datetime.date.today()
    end = today + datetime.timedelta(days=REPLAY_LOOKAHEAD_DAYS)
    id_map = _espn_to_nflverse(session, today.isoformat())
    events = _fetch_events(session, config, {
        "dates": "{}-{}".format(today.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
        "limit": 1000})
    out = {}
    for e in events:
        game_id = id_map.get(str(e.get("id")))
        if game_id in pks and is_final(e):
            out[game_id] = (e.get("date") or "")[:10]
    return out


def is_final(event):
    """A game that actually finished, with both scores readable.

    Reads the status type's `completed` flag rather than matching a status NAME
    or a detail string -- the lesson cfb_grading records, and it applies here
    for the same reason: an overtime game keeps name=STATUS_FINAL and moves the
    overtime into detail="Final/OT".

    The score check is not redundant with the flag: a game abandoned for
    weather can carry partial scores with completed=false, and a data glitch
    can carry the flag with nulls. Both must fail this rather than grade."""
    if not _status(event).get("completed"):
        return False
    sides = _sides(event)
    return bool(sides) and all(s["score"] is not None for s in sides.values())


def is_called_off(event):
    """Postponed, cancelled, abandoned or suspended -- no result on this date."""
    return _status(event).get("name") in CALLED_OFF_STATES


def live_state(event):
    """A short human status for a game that has not finished."""
    st = _status(event)
    detail = st.get("shortDetail") or st.get("detail") or st.get("description")
    if detail:
        return detail
    date = event.get("date") or ""
    return "Scheduled (kickoff {}Z)".format(date[11:16]) if len(date) >= 16 else "Scheduled"


def observed_facts(event):
    """The raw numbers a verdict can be RE-DERIVED from later, stored per pick,
    so a rule that changes can be re-applied to rows written under the old one."""
    if event is None or not is_final(event):
        return None
    sides = _sides(event)
    home, away = sides["home"]["score"], sides["away"]["score"]
    return {
        "away_score": away, "home_score": home,
        "result": "H" if home > away else ("A" if away > home else "T"),
        "state": _status(event).get("description"),
    }


def _picked_abbr(side):
    """The club a side names -- the leading token, the same rule insights.js's
    sideColor uses, so the card and the ledger cannot disagree about whose pick
    it was."""
    return str(side or "").split(" ")[0]


def grade(pick, event, assume_lines):
    """(result_text, verdict, basis) for one NFL pick against one game.

    `assume_lines` is accepted and ignored: it exists for MLB totals, which
    need a number this repo holds no prices for. Moneyline resolves off the
    result alone, so no UNPRICED verdict is reachable.
    """
    bet_type, side = pick["bet_type"], pick["side"]
    away, home = pick["away_abbr"], pick["home_abbr"]

    if event is None:
        return "not on this date's schedule", "UNRESOLVED", None
    if is_called_off(event):
        st = _status(event)
        text = st.get("description") or "Postponed"
        if pick.get("replayed_on"):
            text += " → played {}".format(pick["replayed_on"][5:])
        return text, "POSTPONED", None
    if not is_final(event):
        return live_state(event), "PENDING", None

    picked = _picked_abbr(side)
    # A pick's side must name a club in this game. If it does not, say so --
    # never fall through to a comparison that scores a malformed side as a
    # clean MISS, the guard every other sport's grade() applies.
    if picked not in (away, home):
        return "side {!r} names neither club".format(side), "UNRESOLVED", None
    if bet_type != "moneyline":
        return "bet type {!r} has no NFL grading rule".format(bet_type), "UNRESOLVED", None

    facts = observed_facts(event)
    result = facts["result"]
    text = "{} {}-{} {}".format(away, facts["away_score"], facts["home_score"], home)

    # A TIE IS A PUSH, NOT A LOSS AND NOT AN ERROR. NFL overtime does not have
    # to produce a winner and about one game a season ends level; every book
    # returns the stake on a tied moneyline. cfb_grading returns UNRESOLVED for
    # the same score line because college football abolished ties in 1996, so
    # there it means the feed is wrong. Same shape, opposite meaning -- which
    # is why these two adapters are separate files rather than one shared
    # "football" grader.
    if result == "T":
        return text + " (tie)", "PUSH", "outcome"

    winner = home if result == "H" else away
    return text, "HIT" if winner == picked else "MISS", "outcome"


def list_markets(scored):
    """Delegates to nfl_signals so `--all-markets` ranks NFL rows by the same
    rules the site did. Imported lazily: signal_report builds its adapter
    registry at import time and should not pull the scoring layer in for a run
    that never grades NFL."""
    import nfl_signals
    return nfl_signals.list_markets(scored)


def top_market(scored, threshold):
    import nfl_signals
    return nfl_signals.top_market(scored, threshold)
