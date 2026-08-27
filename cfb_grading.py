"""CFB's grading adapter for signal_report.py.

Same arrangement epl_grading.py has, and for the same reason: signal_report was
written around MLB's StatsAPI shape (codedGameState, linescore innings) and no
ESPN college-football event answers to any of it. This file supplies CFB's
implementations of the small surface the grading rules need, and signal_report
dispatches through SPORT_ADAPTERS. MLB's and EPL's entries in that registry are
their own functions, unchanged and uncalled from here.

WHAT MAKES CFB DIFFERENT FROM THE OTHER TWO, and why each of these is a rule
rather than an accident:

  ONE MARKET, ONE OUTCOME. Moneyline only, and college football has had
  overtime since 1996 -- a regulation tie goes to OT and OT does not end tied.
  So unlike EPL there is no draw to place inside or outside the bet, and unlike
  MLB there is no total that can land on the number. HIT and MISS are the only
  outcome verdicts reachable, and a tie in the data is treated as UNRESOLVED
  rather than graded, because it means the source is wrong and not that the
  game ended level.

  THE STORE SPANS A WEEK. fetchers/cfb.build_game_entities looks seven days
  ahead (college football is a Saturday sport; a one-date slate leaves the tab
  empty most of the week), so the store holds fixtures for dates other than the
  one being graded. store_spans_dates is True in the adapter for exactly this,
  which makes a pick whose game is not on the graded date DEFERRED rather than
  UNRESOLVED -- the same correction EPL needed.

  THE TEAM STRING IS RESOLVED, NOT READ. A pick's side carries the abbreviation
  fetchers/cfb._team_ref produced from team_meta, which is NOT always ESPN's own
  `team.abbreviation`. Rather than compare two abbreviation vocabularies, both
  sides of the comparison are put through _team_ref, so the pick and the event
  cannot disagree about a program's name by construction.

The verdict vocabulary is signal_report's (HIT / MISS / PUSH / PENDING /
POSTPONED / UNRESOLVED / UNPRICED) and this module returns nothing outside it.
PUSH and UNPRICED are unreachable, per the paragraph above.
"""

import datetime

REQUEST_TIMEOUT = 30
# ESPN status names that mean "no result on this date". Weather is the common
# one in college football (a lightning delay that runs out of daylight), and a
# cancelled non-conference game is never rescheduled at all.
CALLED_OFF_STATES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED",
                     "STATUS_ABANDONED", "STATUS_SUSPENDED"}
# How far ahead to look for a postponed game that was replayed. Shorter than
# EPL's 120 days on purpose: a college season is thirteen weeks with no midweek
# replay slots, so a postponed game is either made up within a few weeks or
# never played. Looking half a year ahead would only ever find next season.
REPLAY_LOOKAHEAD_DAYS = 45


def _scoreboard_url(config=None):
    """(url, fbs_group) for ESPN's college-football scoreboard.

    Read from config.yaml's `cfb` block, matching how epl_grading gets its
    endpoint, and FALLING BACK to fetchers/cfb's own constants. The fallback is
    not defensive padding: that module needs the same endpoint for the schedule
    fallback and declares it as a module constant, so there are legitimately two
    consumers. tools/verify/test_cfb_grading.py asserts the two agree, which is
    what stops them drifting -- a silent divergence would have the grader
    reading a different endpoint from the builder."""
    from fetchers import cfb
    cfg = (config or {}).get("cfb") or {}
    return (cfg.get("scoreboard_url") or cfb.ESPN_CFB_SCOREBOARD,
            cfg.get("fbs_group", cfb.ESPN_FBS_GROUP))


def _team_abbr(school):
    from fetchers import cfb
    return cfb._team_ref(school)["abbr"]


def _status(event):
    comp = (event.get("competitions") or [{}])[0]
    return (comp.get("status") or {}).get("type") or {}


def _sides(event):
    """{"home": {...}, "away": {...}} with abbr and int score, or {} if the
    event is not a readable two-sided game.

    `abbr` comes from _team_abbr, NOT from ESPN's own abbreviation field -- see
    the module docstring."""
    comp = (event.get("competitions") or [{}])[0]
    out = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        try:
            score = int(c.get("score"))
        except (TypeError, ValueError):
            score = None
        school = team.get("location")
        out[c.get("homeAway")] = {"abbr": _team_abbr(school), "name": school,
                                  "score": score}
    return out if set(out) == {"home", "away"} else {}


def fetch_slate(session, config, date):
    """{gamePk: event} for one date, keyed by ESPN's event id as a string.

    THAT KEY IS ALSO CFBD'S. The store is written with whatever `game_id` the
    schedule carried, and the two possible schedule sources turn out to share
    an id space: on 2025-11-15 all 25 ESPN events matched a cfbfastR game_id
    exactly. So one lookup serves picks made from either source, and a stored
    pick does not have to record which one built it.

    ONE DATE, not the builder's seven-day window -- the grader is asked about a
    specific date and must not pull in a neighbouring Saturday. Picks for the
    rest of the window are handled by store_spans_dates, not by widening this.
    """
    url, group = _scoreboard_url(config)
    r = session.get(url, params={"dates": date.replace("-", ""),
                                 "groups": group, "limit": 1000},
                    timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return {str(e["id"]): e for e in r.json().get("events") or [] if e.get("id")}


def fetch_replay_dates(session, config, pks):
    """{gamePk: date} for a postponed game that has since been played.

    One ranged call rather than one per pk, same as EPL's. See
    REPLAY_LOOKAHEAD_DAYS for why the range is much shorter here.
    """
    if not pks:
        return {}
    url, group = _scoreboard_url(config)
    today = datetime.date.today()
    end = today + datetime.timedelta(days=REPLAY_LOOKAHEAD_DAYS)
    r = session.get(url, params={"dates": "{}-{}".format(today.strftime("%Y%m%d"),
                                                         end.strftime("%Y%m%d")),
                                 "groups": group, "limit": 1000},
                    timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = {}
    for e in r.json().get("events") or []:
        pk = str(e.get("id"))
        if pk in pks and is_final(e):
            out[pk] = (e.get("date") or "")[:10]
    return out


def is_final(event):
    """A game that actually finished, with both scores readable.

    Reads the status type's `completed` flag rather than matching a status
    NAME or a detail string, the same lesson epl_grading records. On this
    endpoint an overtime game keeps name=STATUS_FINAL and moves the overtime
    into detail="Final/OT" (verified on a real one: SMU 26-20 Miami, in the
    fixture) -- so a grader matching on `detail == "Final"` would defer every
    overtime result, and college football reaches overtime often. The flag is
    correct in both cases and costs nothing.

    The score check is not redundant with the flag: a game called for weather
    can carry partial scores with completed=false, and a data glitch can carry
    the flag with nulls. Both must fail this rather than grade as a result.
    """
    if not _status(event).get("completed"):
        return False
    sides = _sides(event)
    return bool(sides) and all(s["score"] is not None for s in sides.values())


def is_called_off(event):
    """Postponed, cancelled, abandoned or suspended -- never a result on this
    date."""
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
    """The raw numbers a verdict can be RE-DERIVED from later, stored per pick.

    Same purpose as MLB's and EPL's: a rule that changes later must be
    re-applicable to rows written under the old one. `result` is stored
    alongside the scores even though it is derivable from them, because it is
    what the grading rule branches on.
    """
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
    """The program a side names -- the leading token, the same rule
    insights.js's sideColor uses to tint a market chip, so the card and the
    ledger cannot disagree about whose pick it was. CFB sides are a bare abbr
    today; this keeps working if a compound side is ever added."""
    return str(side or "").split(" ")[0]


def grade(pick, event, assume_lines):
    """(result_text, verdict, basis) for one CFB pick against one game.

    `assume_lines` is accepted and ignored: it exists for MLB totals, which
    need a number this repo holds no prices for. Moneyline resolves off the
    result alone, so no UNPRICED verdict is reachable here.
    """
    bet_type, side = pick["bet_type"], pick["side"]
    away, home = pick["away_abbr"], pick["home_abbr"]

    if event is None:
        return "not on this date's schedule", "UNRESOLVED", None
    if is_called_off(event):
        st = _status(event)
        text = st.get("description") or "Postponed"
        if pick.get("replayed_on"):
            # Month-day only: the year is in the report header already and the
            # full ISO date pushes this row past the RESULT column.
            text += " → played {}".format(pick["replayed_on"][5:])
        return text, "POSTPONED", None
    if not is_final(event):
        return live_state(event), "PENDING", None

    picked = _picked_abbr(side)
    # A pick's side must name a program in this game. If it does not, say so --
    # never fall through to a comparison that would score a malformed side as a
    # clean MISS, which is the guard MLB's and EPL's grade() both apply.
    if picked not in (away, home):
        return "side {!r} names neither program".format(side), "UNRESOLVED", None
    if bet_type != "moneyline":
        return "bet type {!r} has no CFB grading rule".format(bet_type), "UNRESOLVED", None

    facts = observed_facts(event)
    result = facts["result"]
    text = "{} {}-{} {}".format(away, facts["away_score"], facts["home_score"], home)

    # A TIE IS NOT A RESULT IN THIS SPORT. Overtime has settled every regulation
    # tie since 1996, so a level final score means the source is wrong, not that
    # the game ended level. Grading it MISS would write a confident wrong
    # verdict into an append-only ledger; UNRESOLVED says what is actually true.
    if result == "T":
        return text + " (tie -- not possible in regulation+OT; source suspect)", "UNRESOLVED", None

    winner = home if result == "H" else away
    return text, "HIT" if winner == picked else "MISS", "outcome"


def list_markets(scored):
    """Delegates to cfb_signals so `--all-markets` ranks CFB rows by the same
    rules the site did. Imported lazily: signal_report builds its adapter
    registry at import time and should not pull the scoring layer in for a run
    that never grades CFB."""
    import cfb_signals
    return cfb_signals.list_markets(scored)


def top_market(scored, threshold):
    import cfb_signals
    return cfb_signals.top_market(scored, threshold)
