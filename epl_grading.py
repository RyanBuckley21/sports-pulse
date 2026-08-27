"""EPL's grading adapter for signal_report.py.

signal_report was written entirely around MLB's StatsAPI shape -- codedGameState,
linescore innings, per-inning run splits -- and there is no version of those an
ESPN soccer event answers to. So rather than branch that module's grading rules
per sport, this file supplies EPL's implementations of the small surface those
rules actually need, and signal_report dispatches through SPORT_ADAPTERS. MLB's
entries in that registry are its existing functions, unchanged and uncalled from
here: nothing in this module can alter how an MLB row is graded.

THE ONE RULE THAT MATTERS, and the reason a shared grader could not have been
bent into shape: A DRAW GRADES DIFFERENTLY PER MARKET.

    double_chance ("ARS or Draw")  -- a draw is a HIT. The draw is inside the bet.
    match_result  ("ARS")          -- a draw is a MISS. Not a push; 1X2 has no push.

Grading both as "did the named side win" would silently mark every drawn
double-chance pick as a loss, which is 23.6% of matches and would make the
ledger read as a losing system while the market it describes was winning. The
verdict vocabulary is signal_report's (HIT / MISS / PUSH / PENDING / POSTPONED /
UNRESOLVED / UNPRICED) and this module returns nothing outside it.

NO PUSH EXISTS IN EITHER MARKET. Both resolve on every completed match, so PUSH
is unreachable here -- unlike MLB, where a total landing exactly on the number
pushes. Stated because its absence is a fact about the sport, not an omission.
"""

import datetime

REQUEST_TIMEOUT = 30
# ESPN status names that mean "not played on this date". A soccer fixture can be
# postponed (weather, cup replays, mid-season disruption) or abandoned after
# kickoff, and both must grade POSTPONED rather than as a result.
CALLED_OFF_STATES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED",
                     "STATUS_ABANDONED", "STATUS_SUSPENDED"}


def _status(event):
    comp = (event.get("competitions") or [{}])[0]
    return (comp.get("status") or {}).get("type") or {}


def _sides(event):
    """{"home": {...}, "away": {...}} with abbr and int score, or {} if the event
    is not a readable two-sided match."""
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


def fetch_slate(session, config, date):
    """{gamePk: event} for one date, keyed by ESPN's event id as a string --
    the same key fetchers/epl.build_game_entities writes into the store, which
    is what lets signal_report match a stored pick to its match.

    ONE DATE, not the builder's three-day fixture window. The builder looks
    ahead so the Games tab is not empty midweek; the grader is asked about one
    specific date and must not pull in neighbouring matchdays, or a pick would
    be graded against a fixture from a different day.
    """
    url = (config.get("epl") or {}).get("scoreboard_url")
    if not url:
        return {}
    compact = date.replace("-", "")
    r = session.get(url, params={"dates": compact, "limit": 1000},
                    timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return {str(e["id"]): e for e in r.json().get("events") or [] if e.get("id")}


def fetch_replay_dates(session, config, pks):
    """{gamePk: date} for a postponed fixture that has since been played.

    MLB keeps a postponed game's gamePk and reappears on the replay date, and
    ESPN does the same for a rearranged fixture, so the shape of the answer is
    the same -- only the search is different. There is no bulk id lookup on this
    endpoint, so this scans the following 120 days once and matches on id: a
    rearranged Premier League fixture is normally replayed within weeks, and one
    call is cheaper than one per pk.
    """
    if not pks:
        return {}
    url = (config.get("epl") or {}).get("scoreboard_url")
    if not url:
        return {}
    today = datetime.date.today()
    end = today + datetime.timedelta(days=120)
    r = session.get(url, params={"dates": "{}-{}".format(today.strftime("%Y%m%d"),
                                                         end.strftime("%Y%m%d")),
                                 "limit": 1000}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = {}
    for e in r.json().get("events") or []:
        pk = str(e.get("id"))
        if pk in pks and is_final(e):
            out[pk] = (e.get("date") or "")[:10]
    return out


def is_final(event):
    """A match that actually finished, with both scores readable.

    Reads the status type's `completed` flag rather than matching a status NAME,
    which is worldcup.py's and fetchers/epl.py's hard-won lesson: a match decided
    beyond 90 minutes reports STATUS_FINAL_AET or STATUS_FINAL_PENALTIES instead
    of STATUS_FULL_TIME. League matches rarely reach that, but the flag is
    correct regardless and costs nothing.

    The score check is not redundant with the flag: an abandoned match can carry
    completed=false with partial scores, and a data glitch can carry the flag
    with nulls. Both must fail this rather than grade as a result.
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
    """A short human status for a match that has not finished."""
    st = _status(event)
    detail = st.get("shortDetail") or st.get("detail") or st.get("description")
    if detail:
        return detail
    date = event.get("date") or ""
    return "Scheduled (kickoff {}Z)".format(date[11:16]) if len(date) >= 16 else "Scheduled"


def observed_facts(event):
    """The raw numbers a verdict can be RE-DERIVED from later, stored per pick.

    Same purpose as MLB's: a rule that changes later must be re-applicable to
    rows written under the old one. `result` is stored alongside the scores even
    though it is derivable from them, because it is the value both grading rules
    actually branch on and a reader auditing a drawn match should not have to
    re-derive the draw.
    """
    if event is None or not is_final(event):
        return None
    sides = _sides(event)
    home, away = sides["home"]["score"], sides["away"]["score"]
    return {
        "away_score": away, "home_score": home,
        "result": "H" if home > away else ("A" if away > home else "D"),
        "state": _status(event).get("description"),
    }


def _picked_abbr(side):
    """The club a side names. "ARS" and "ARS or Draw" both resolve to "ARS" --
    the leading token, the same rule insights.js's sideColor uses to tint a
    market chip, so the card and the ledger cannot disagree about whose pick it
    was."""
    return str(side or "").split(" ")[0]


def grade(pick, event, assume_lines):
    """(result_text, verdict, basis) for one EPL pick against one match.

    `assume_lines` is accepted and ignored: it exists for MLB totals, which need
    a number that this repo holds no prices for. Neither EPL market needs a line
    -- both resolve off the result alone -- so there is nothing to assume and no
    UNPRICED verdict is reachable here.
    """
    bet_type, side = pick["bet_type"], pick["side"]
    away, home = pick["away_abbr"], pick["home_abbr"]

    if event is None:
        return "not on this date's schedule", "UNRESOLVED", None
    if is_called_off(event):
        st = _status(event)
        text = st.get("description") or "Postponed"
        if pick.get("replayed_on"):
            # Month-day only: the year is already in the report header and the
            # full ISO date pushes this row past the RESULT column.
            text += " → played {}".format(pick["replayed_on"][5:])
        return text, "POSTPONED", None
    if not is_final(event):
        return live_state(event), "PENDING", None

    picked = _picked_abbr(side)
    # A pick's side must name a club in this match. If it does not, say so --
    # never fall through to a comparison that would score a malformed side as a
    # clean MISS, which is the same guard MLB's grade() applies.
    if picked not in (away, home):
        return "side {!r} names neither club".format(side), "UNRESOLVED", None
    if bet_type not in ("double_chance", "match_result"):
        return "bet type {!r} has no EPL grading rule".format(bet_type), "UNRESOLVED", None

    facts = observed_facts(event)
    result = facts["result"]
    winner = home if result == "H" else (away if result == "A" else None)
    text = "{} {}-{} {}".format(away, facts["away_score"], facts["home_score"], home)

    if bet_type == "double_chance":
        # The draw is INSIDE this bet. Grading it as "did the named side win"
        # would mark 23.6% of matches as losses on a market that won them.
        hit = (winner == picked) or (result == "D")
        return (text + (" (draw)" if result == "D" else ""),
                "HIT" if hit else "MISS", "outcome")

    # match_result: the side outright. A draw LOSES -- 1X2 has no push, so this
    # must not return PUSH however much it resembles one.
    return (text + (" (draw)" if result == "D" else ""),
            "HIT" if winner == picked else "MISS", "outcome")


def list_markets(scored):
    """Delegates to epl_signals so `--all-markets` ranks EPL rows by the same
    rules the site did. Imported lazily: signal_report builds its adapter
    registry at import time and should not pull the scoring layer in for a run
    that never grades EPL."""
    import epl_signals
    return epl_signals.list_markets(scored)


def top_market(scored, threshold):
    import epl_signals
    return epl_signals.top_market(scored, threshold)
