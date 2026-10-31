"""NBA data layer: ESPN's scoreboard, filtered to the games that count.

REGISTERED, NOT BUILT. This module is the first of three NBA steps and is
imported by nothing in the pipeline yet: no GAME_BUILDERS entry, no
SPORT_FETCHERS entry, in neither gate. What is here is the part every later
step stands on and that fails silently if it is wrong -- which events are
real games, which day they belong to, and the two prior-season quantities the
cold-start tier will need. The steps after it, in order:

  2. a backtest over past seasons (nba_backtest.py) that measures each
     signal's reliability, then nba_signals.py and build_game_entities with
     weights taken from it, registered in GAME_BUILDERS and SPORT_ADAPTERS
     but still in neither gate;
  3. activation: the gates, a continue-on-error grading step, the alarm list.

No weight exists anywhere for NBA, by design (invariant 8): a builder cannot
produce a standout without one, so it waits for the backtest rather than
shipping a placeholder the way NFL's registration once did.

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
