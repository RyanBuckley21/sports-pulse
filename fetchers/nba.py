"""NBA: ESPN's scoreboard filtered to the games that count, and the games builder.

THE PICKS PATH IS BUILT (2026-11-11); THERE ARE NO LEADERBOARDS. This module
began as the first of three NBA steps -- which events are real games, which
day they belong to, and the prior-season quantities the cold-start tier
needs. Step 2 ran on 2026-11-11: nba_backtest.py --fit measured the weights,
nba_signals.py scores on them, and build_game_entities (at the foot of this
file) is GAME_BUILDERS["nba"]. Step 3, activation, is `nba` in
active_game_sports. What is NOT here is a player-stats fetcher: NBA has no
SPORT_FETCHERS entry and must not be added to active_sports.

Every weight NBA uses is in config.yaml's betting_signals.nba block with its
derivation (invariant 8). Nothing in this file carries one.

WHAT COUNTS AS A GAME, measured on 2025-26 and pinned in test_nba:

    keep  season.type == 2  AND  competitions[0].type.id == "1" (STD)

  Neither half is enough. season.type 2 also admits All-Star weekend
  (type ALLSTAR, id 4: teams STARS / WORLD / STRIPES) and the NBA Cup final
  (type CC, id 39: NY vs SA, 2025-12-16, outside the 82). type STD also
  admits PRESEASON (season.type 1: 67 of October 2026's 155 events).
  The rule alone gives 1,232-1,235 a season, NOT 1,230: a POSTPONED game
  passes it, because ESPN keeps the called-off shell beside the makeup (2, 5
  and 4 shells in 2023-24..2025-26; eight teams read 83 counted games in
  2025-26). The shell reports state "post" with completed False and scores
  "0"/"0" (tools/verify/nba_postponed_fixture.json, all four of 2025-26's).
  Only the DECIDED subset is exactly 1,230 games, 30 teams, 82 each, in every
  one of the three seasons -- so anything that counts games, rates or form
  must filter decided first (_decided, used by home_margins and
  build_prior_season_margin). Unfiltered a
  2025-26 walk gives 1,235 games and 33 "teams" -- and an All-Star blowout
  folded into a team's form is a confident wrong number nobody would see.

  neutralSite IS NOT A FILTER. Five games that count toward the 82 sit at
  neutral sites -- the two Cup semifinals and the Mexico City, Berlin and
  London games -- while All-Star games report neutralSite False. Using it
  would drop real games and keep fake ones.

  Measured by a separate session with ESPN access, 2026-10-30/31; this
  sandbox cannot reach site.api.espn.com. The fixtures carry every case.

WHICH DAY A GAME IS ON is Eastern (invariant 3). ESPN stamps events in UTC,
and NBA tips at 7:00-10:30 PM Eastern, so most evening games carry the NEXT
day's UTC date: the 2025 Mexico City game is "2025-11-02T02:00Z" and was
played on November 1. Read naively, back-to-backs look like same-day
doubleheaders (CHI at MIA, 2026-02-01T01:00Z and T23:00Z are Jan 31 and
Feb 1). Every date here goes through slate_clock.

THE SEASON IS NAMED FOR ITS ENDING YEAR: 2026-27 is season.year 2027, so the
prior season to a game in October 2026 is 2026. Unlike nflverse, which names a
season for the year it starts -- see season_year_for_date.

PRICES ARE LIVE-ONLY AND INTERMITTENT (measured 2026-10-28..31): ESPN strips a
game's odds block when it completes (0 of 12 completed on 10-28, 0 of 170 in
February 2026), and before tip it may or may not carry one (10 of 10 a day
ahead on 10-29; 0 of 15 for 11-02 on 10-31). There is no NBA price history on
this endpoint at all, so price capture must be sticky and close to tip, and
odds_first_seen is the only CLV baseline NBA will ever have.
"""

import datetime

import espn_dates
import slate_clock

REQUEST_TIMEOUT = 30
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"

# The two halves of the counted-game rule. Kept as named constants because the
# test pins them against the fixture's own case labels, not against themselves.
REGULAR_SEASON_TYPE = 2       # event.season.type: 1 preseason, 2 regular, 3 postseason
STANDARD_GAME_TYPE_ID = "1"   # competitions[0].type.id: STD. ALLSTAR is 4, CC is 39.

# A regular season runs October to mid-April. Walking October of the year
# before through June covers it with margin at both ends (the counted-game rule,
# not the window, is what excludes preseason and playoffs): nine month
# requests, inside espn_dates.MAX_MONTHS. The largest month seen was 239
# events, well inside SCOREBOARD_LIMIT.
SEASON_FIRST_MONTH = 10
SEASON_LAST_MONTH = 6


def season_year_for_date(day):
    """ESPN's season.year for a date: the year the season ENDS.

    A game on 2026-10-30 is season 2027; one on 2026-02-15 is season 2026.
    August is the cut: no NBA game is played in July or August, so anything
    from August on belongs to the season about to start. Checked against every
    event in nba_games_fixture.json, whose season.year is ESPN's own."""
    return day.year + 1 if day.month >= 8 else day.year


def _comp(event):
    return (event.get("competitions") or [{}])[0]


def is_counted_game(event):
    """Whether an event is a regular-season game that counts toward the 82.
    See the module docstring for why it takes BOTH fields and why neutralSite
    plays no part."""
    if (event.get("season") or {}).get("type") != REGULAR_SEASON_TYPE:
        return False
    return str((_comp(event).get("type") or {}).get("id")) == STANDARD_GAME_TYPE_ID


def slate_date(event):
    """The Eastern date a game is played on, YYYY-MM-DD, or None if the event
    carries no usable timestamp. Never the UTC date -- see the docstring."""
    stamp = event.get("date") or ""
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return slate_clock.eastern_date(when)


def parse_game(event):
    """One scoreboard event -> the flat game row everything downstream reads,
    or None for an event that is not a readable two-sided game.

    Scores are None until the game is COMPLETED: ESPN sends "0" for a game not
    yet played, and a 0-0 read as a result would be a tie in a sport that
    cannot have one."""
    comp = _comp(event)
    sides = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        sides[c.get("homeAway")] = (team.get("abbreviation"), c.get("score"))
    if set(sides) != {"home", "away"} or not all(abbr for abbr, _ in sides.values()):
        return None
    completed = bool(((comp.get("status") or {}).get("type") or {}).get("completed"))

    def score(raw):
        if not completed:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    return {
        "id": str(event.get("id")),
        "slate_date": slate_date(event),
        "season_year": (event.get("season") or {}).get("year"),
        "home": sides["home"][0], "away": sides["away"][0],
        "home_score": score(sides["home"][1]), "away_score": score(sides["away"][1]),
        "completed": completed,
        "neutral_site": bool(comp.get("neutralSite")),
    }


def counted_games(events):
    """The parsed rows of every counted game among `events`, in feed order."""
    out = []
    for e in events:
        if is_counted_game(e):
            row = parse_game(e)
            if row is not None:
                out.append(row)
    return out


def _decided(row):
    return (row["completed"] and row["home_score"] is not None
            and row["away_score"] is not None)


def get_events(session, params, url=SCOREBOARD_URL):
    """One scoreboard request. Through espn_dates.checked_events, which raises
    on a full page rather than hand back a possibly truncated list."""
    r = session.get(url, params=dict(params, limit=espn_dates.SCOREBOARD_LIMIT),
                    timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return espn_dates.checked_events(r.json(), what="ESPN NBA scoreboard")


def fetch_season_games(session, season_year, url=SCOREBOARD_URL):
    """Every counted game of one season, month by month (ESPN rejects date
    ranges -- invariant 11). Raises on any failed month rather than returning
    a season with a hole in it: a form built from eleven months of a twelve-
    month walk is wrong without looking wrong."""
    start = datetime.date(season_year - 1, SEASON_FIRST_MONTH, 1)
    end = datetime.date(season_year, SEASON_LAST_MONTH, 30)
    events = espn_dates.fetch_window(
        lambda params: {"events": get_events(session, params, url)}, start, end)
    seen, out = set(), []
    for row in counted_games(events):
        if row["id"] not in seen:            # a game listed in two months' answers
            seen.add(row["id"])
            out.append(row)
    return out


def home_margins(games):
    """(home score - away score) for every decided, counted game at a TRUE home
    venue -- the sample a home-court shift is measured on.

    Neutral sites are excluded here, and only here: they are real games and
    count toward a team's form, but neither team is at home. On 2025-26 the
    exclusion moves nothing measurable (2025-26: +1.727 true-home vs +1.737
    all). Pooled over 2023-24..2025-26, 3,678 true-home decided games: +1.854
    pts [+1.316, +2.370], home win rate 0.5473 (4,000 resamples, seed 17;
    re-derived 2026-10-31 from tools/verify/nba_seasons_2024_2026.json, which
    this function's own module built). Every season sits well under the ~3
    points NBA home court is commonly quoted at, which is why the shift is
    fitted by the backtest and never assumed."""
    return [r["home_score"] - r["away_score"] for r in games
            if _decided(r) and not r["neutral_site"]]


def build_prior_season_margin(games):
    """{team: {"margin": points margin per game, "games": n}} over decided,
    counted games -- the cold-start tier's one number, the NBA counterpart of
    fetchers/nfl.build_prior_season_margin.

    Neutral-site games ARE included: a team's margin is its own, wherever it
    played. NO games floor is applied here, unlike NFL's: a full prior season
    gives every team 82, and the count is returned so the backtest can choose a
    floor from evidence instead of this module guessing one."""
    acc = {}
    for r in games:
        if not _decided(r):
            continue
        diff = r["home_score"] - r["away_score"]
        for team, m in ((r["home"], diff), (r["away"], -diff)):
            acc.setdefault(team, []).append(m)
    return {t: {"margin": sum(ms) / len(ms), "games": len(ms)} for t, ms in acc.items()}


# ---------------------------------------------------------------------------
# THE GAMES BUILDER (2026-11-11): generate_insights.GAME_BUILDERS["nba"].
#
# One date's games, not a week's. The NBA plays almost every day, so unlike the
# NFL (Thursday to Monday in one store) a slate here IS a date -- the Eastern
# one (invariant 3). On a day with no games the next slate within
# slate_clock's lookahead is served instead, and said so in the log.
#
# WHAT A RUN COSTS: one request per month from October through the slate's
# month (two in November, nine by June), and nine more for last season ONLY
# while some club is still under the games floor. No key, no quota.
#
# WHAT EACH GAME IS SCORED ON: both clubs' points margin per game from games
# played STRICTLY BEFORE the slate date, which is exactly what
# nba_backtest.point_in_time gives the backtest -- a game on the slate can
# never contribute to its own lean.
#
# PRICES come off the same event the game was read from (espn_odds.parse_event)
# and go on the entity for display, the Bets tab and CLV only. They are never
# an input to the score (invariant 1). ESPN strips them when the game ends, so
# the store's sticky rule and nba_price_capture.py are what keep them.
# ---------------------------------------------------------------------------
SEASON_FLOOR = 5    # games a club needs before its season margin is used. The
                    # backtest's number (nba_backtest.SEASON_FLOOR), measured
                    # flat across 5/10/20; test_nba_live pins the two equal.

# Printed on every card that carries a pick. The lean knows the standings and
# nothing about tonight: NBA teams rest stars on an injury report published
# hours before tip, which this pipeline does not read (no verified free feed).
INJURY_CAUTION = ("This lean does not know tonight's injury report. Check who "
                  "is resting before backing it.")
COLD_START_CAUTION = ("Early read: a club has played fewer than {} games, so this "
                      "lean is on last season's margin.").format(SEASON_FLOOR)


def fetch_events_through(session, season_year, through, url=SCOREBOARD_URL):
    """Every raw scoreboard event from the season's first month through the
    month containing `through` (a date), de-duplicated by id. Raises on any
    failed month, for fetch_season_games' reason: a form built from a season
    with a hole in it is wrong without looking wrong."""
    start = datetime.date(season_year - 1, SEASON_FIRST_MONTH, 1)
    events = espn_dates.fetch_window(
        lambda params: {"events": get_events(session, params, url)}, start, through)
    seen, out = set(), []
    for e in events:
        key = str(e.get("id"))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def season_margins(rows, before):
    """{team: {"margin", "games"}} over decided counted games played STRICTLY
    BEFORE `before` (YYYY-MM-DD). Neutral-site games count: a club's margin is
    its own wherever it played (build_prior_season_margin, same rule)."""
    return build_prior_season_margin(
        [r for r in rows if r.get("slate_date") and r["slate_date"] < before])


def _usable(margins, team):
    m = (margins or {}).get(team)
    return m["margin"] if m and m["games"] >= SEASON_FLOOR else None


def matchup_inputs(home, away, neutral_site, season, prior):
    """nba_signals' input dict for one matchup from the two margin tables.

    The ONE place the live path decides what a game is scored on, so that
    test_nba_live can hold it against nba_backtest.score_record game by game:
    a builder that fed the scorer anything other than what the backtest
    measured would ship numbers fitted to a different model.

    A season margin is used only when BOTH clubs have one. A 12-game club
    against a 3-game club is not a comparison of like with like, and the
    backtest's season tier is defined the same way (both at the floor)."""
    import nba_signals
    home_season, away_season = _usable(season, home), _usable(season, away)
    if home_season is None or away_season is None:
        home_season = away_season = None
    return nba_signals.build_inputs(
        away, home, away_season_margin=away_season, home_season_margin=home_season,
        away_prior_margin=((prior or {}).get(away) or {}).get("margin"),
        home_prior_margin=((prior or {}).get(home) or {}).get("margin"),
        neutral_site=neutral_site)


def _tip_label(event):
    """'7:30 PM ET' from the event's UTC timestamp, or None."""
    stamp = event.get("date") or ""
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        local = when.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # no tzdata: the same fallback slate_clock uses
        local = when.astimezone(slate_clock._FALLBACK)
    hour = local.hour % 12 or 12
    return "{}:{:02d} {} ET".format(hour, local.minute, "AM" if local.hour < 12 else "PM")


def _team_ref(abbr):
    import team_meta  # local import, the convention nfl.py and cfb.py follow
    meta = team_meta.get_team_meta("nba", abbr) or {}
    return {"abbr": meta.get("abbr") or abbr, "name": abbr, "color": meta.get("color")}


def _fmt_margin(value):
    return "n/a" if value is None else "{:+.1f}".format(value)


def _status_label(event):
    state = ((_comp(event).get("status") or {}).get("type") or {}).get("state")
    return {"pre": "Preview", "in": "Live", "post": "Final"}.get(state, "Preview")


def build_one_game(config, event, season, prior):
    """One slate event -> the game entity the site, the store and the grader
    read. `season` and `prior` are {team: {"margin", "games"}}."""
    import espn_odds
    import nba_signals
    row = parse_game(event)
    if row is None:
        raise ValueError("event {} is not a readable two-sided game".format(event.get("id")))
    home, away = row["home"], row["away"]
    inputs = matchup_inputs(home, away, row["neutral_site"], season, prior)
    home_season, away_season = inputs["home_season_margin"], inputs["away_season_margin"]
    home_prior, away_prior = inputs["home_prior_margin"], inputs["away_prior_margin"]
    betting = nba_signals.score_game(config, "nba", inputs)
    cfg = (config.get("betting_signals") or {}).get("nba") or {}
    standout = nba_signals.top_market(betting, cfg.get("standout_threshold", 0))

    labels = ((config.get("insights_ui") or {}).get("nba") or {}).get("market_labels") or {}
    signal_scores = [{"market": labels.get(m["bet_type"], m["bet_type"]),
                      "side": m["side"], "score": m["score"]}
                     for m in nba_signals.list_markets(betting)]
    if standout:
        standout = {**standout, "market": labels.get(standout.get("bet_type"), standout.get("bet_type"))}

    tier = (betting.get("moneyline") or {}).get("tier")
    if tier == "season_margin":
        signals = [{"label": "Points margin / game",
                    "value": "{} {} · {} {}".format(home, _fmt_margin(home_season),
                                                          away, _fmt_margin(away_season)),
                    "tone": "pos" if home_season >= away_season else "neg"}]
    elif tier == "prior_margin":
        signals = [{"label": "Last season margin / game",
                    "value": "{} {} · {} {}".format(home, _fmt_margin(home_prior),
                                                          away, _fmt_margin(away_prior)),
                    "tone": "pos" if (home_prior or 0) >= (away_prior or 0) else "neg"}]
    else:
        signals = []

    cautions = []
    if standout:
        if tier == "prior_margin":
            cautions.append(COLD_START_CAUTION)
        cautions.append(INJURY_CAUTION)

    entity = {
        "gamePk": row["id"],
        "sport": "nba",
        "status": _status_label(event),
        "away": _team_ref(away),
        "home": _team_ref(home),
        "start": _tip_label(event),
        "venue": ((_comp(event).get("venue") or {}).get("fullName")),
        "probables": None,
        "signals": signals,
        "pulse": None,
        "betting_signals": betting,
        "standout": standout,
        "best_angle": standout,
        "caution_notes": cautions or None,
        "signal_scores": signal_scores,
        "compare": None,
        "est_total": None,
        "f5_total": None,
        "context": {
            "away_team": away, "home_team": home,
            "away_season_margin": away_season, "home_season_margin": home_season,
            "away_games": (season.get(away) or {}).get("games", 0),
            "home_games": (season.get(home) or {}).get("games", 0),
            "away_prior_margin": away_prior, "home_prior_margin": home_prior,
            "neutral_site": row["neutral_site"],
            "tier": tier,
        },
    }
    odds = espn_odds.parse_event(event)
    if odds:
        entity["odds"] = odds
    return entity


def build_game_entities(config, game_date, boxscore_cache, team_entities=None,
                        session=None):
    """NBA's entry in generate_insights.GAME_BUILDERS. Same calling convention
    and return shape as the other sports': (entities keyed by ESPN event id,
    {}, []). `boxscore_cache` and `team_entities` are accepted for parity and
    unused -- there is no per-game cache to keep and no team_pulse.nba block.
    `session` exists so a test can serve captured events.

    Raises if the season cannot be read at all, so generate_insights freezes
    the NBA partition instead of clearing it (invariant 4). One unreadable
    game is logged and skipped; the rest of the slate still builds."""
    import espn_odds
    import requests
    session = session or requests.Session()
    day = datetime.date.fromisoformat(game_date)
    season_year = season_year_for_date(day)
    lookahead = day + datetime.timedelta(days=slate_clock.SLATE_LOOKAHEAD_DAYS)
    events = fetch_events_through(session, season_year, lookahead)
    counted = [e for e in events if is_counted_game(e)]
    rows = [r for r in (parse_game(e) for e in counted) if r is not None]
    if not rows:
        return {}, {}, []

    slate = slate_clock.window_start([r["slate_date"] for r in rows], game_date, 0)
    if slate != game_date:
        print("insights(games): nba no games on {} -- showing the next slate instead ({})"
              .format(game_date, slate))
    todays = [e for e in counted if slate_date(e) == slate]
    if not todays:
        return {}, {}, []

    season = season_margins(rows, slate)
    prior = {}
    playing = {t for e in todays for t in ((parse_game(e) or {}).get("home"),
                                           (parse_game(e) or {}).get("away")) if t}
    cold = sorted(t for t in playing if _usable(season, t) is None)
    if cold:
        prior_rows = fetch_season_games(session, season_year - 1)
        prior = build_prior_season_margin(prior_rows)
        print("insights(games): nba cold start -- {} of {} clubs on the slate have "
              "played fewer than {} games ({}), so their games score on the {} "
              "season's margin ({} teams)".format(
                  len(cold), len(playing), SEASON_FLOOR, ", ".join(cold),
                  season_year - 1, len(prior)))

    entities = {}
    for event in todays:
        try:
            ent = build_one_game(config, event, season, prior)
            entities[ent["gamePk"]] = ent
        except Exception as exc:  # noqa: BLE001 -- one bad game must not cost the slate
            print("insights(games): nba game {} failed to build ({}: {}); skipped".format(
                event.get("id"), type(exc).__name__, str(exc)[:160]))
    priced = sum(1 for e in entities.values() if e.get("odds"))
    leans = sum(1 for e in entities.values() if e.get("standout"))
    print("insights(games): nba {} -- {} games, {} with a lean, {} priced".format(
        slate, len(entities), leans, priced))
    cap = ((config.get("betting_signals") or {}).get("nba") or {}).get("max_break_even")
    espn_odds.apply_bettability(entities, cap, sport="nba")
    return entities, {}, []
