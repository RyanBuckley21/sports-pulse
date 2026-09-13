"""CLOSING-LINE CAPTURE, because a hit rate is not an edge.

WHY THIS EXISTS. Every hit rate in this repo answers "which side wins more
often". None of them answer "which side is mispriced", and the two come apart
hardest in college football: 48% of the CFB picks in the ledger were games
decided by 21+ points, where the model went 20-1. Those are not bets. Pricing
one week's board made it concrete -- of 28 priced leans, 23 agreed with the
market favourite at an average break-even of 79.7%, eight of them needing over
90%, and one (Kennesaw at Tennessee, which the model scored 61) priced at
-50000, i.e. 998 wins in 1000 just to stand still. A Signal Score has no idea
whether its 100 is priced at -180 or -8000.

nfl_odds_backtest.py could answer the question for NFL RETROSPECTIVELY because
nflverse's games.csv carries closing moneylines back past 2010. There is no
such archive for college football, and ESPN drops the odds block the moment a
game goes final -- verified: the 2026-09-19 scoreboard carries lines for 54 of
71 events, the 2026-09-12 one for 0 of 80. So the only honest path is to
capture the price BEFORE kickoff and keep it. That is all this module does.

KEYLESS AND CHEAP. The line rides inline on the same ESPN scoreboard the
graders already call -- one request per DATE, not per game -- so a CFB slate
costs one to three requests and an NFL slate one to four. No key, no quota, no
new dependency.

WHAT IT DOES NOT DO. It does not pick, rank, filter or weight anything. No
number from here reaches signal_core, and no model input changes because a
price moved -- that would make the model a line-follower and destroy the only
thing its record currently means. The price is recorded beside the pick and
rendered on the card so a reader can see what the pick costs. Deciding what to
do with that is a person's job.

ONE PROVIDER, NAMED. ESPN returns a single book (DraftKings on every event
observed). It is stored by name rather than averaged into an anonymous
consensus, so a later reader knows exactly whose number this was.
"""

import datetime

REQUEST_TIMEOUT = 20

# ESPN's league path segment. The scoreboard's shape is identical across both,
# which is why one module serves them; a third league is a line in this dict.
LEAGUE_PATHS = {"cfb": "college-football", "nfl": "nfl"}
# CFB only: ESPN's FBS group. Without it the scoreboard returns all divisions.
# Same value fetchers/cfb.py and cfb_grading.py use.
FBS_GROUPS = 80

_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{}/scoreboard"


def american_to_decimal(value):
    """American odds -> decimal payout multiplier. None for anything
    unparseable, which is what a missing line looks like.

    Deliberately the same conversion nfl_odds_backtest.american_to_decimal
    does, kept here rather than imported from it so a backtest script is not a
    runtime dependency of the live path. The two are checked against each other
    in tools/verify/test_odds.py rather than trusted to stay in step."""
    try:
        n = float(str(value).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return 1.0 + (n / 100.0 if n > 0 else 100.0 / abs(n))


def break_even(value):
    """The hit rate a price needs just to stand still, as a fraction.

    This is the number the Signal Score cannot see. -180 needs 64.3%; -8000
    needs 98.8%. It is the RAW implied probability including the book's margin,
    NOT a de-vigged fair probability -- because the question being asked is
    "what must I clear to break even at this price", and the vig is part of
    that. Calling it an implied win probability would be wrong by the width of
    the hold."""
    dec = american_to_decimal(value)
    return None if dec is None else 1.0 / dec


# What a book prints when it has PULLED a market rather than never offered one.
# ESPN passes the string straight through: 10 of the 142 moneyline cells on the
# 2026-09-19 board read exactly this, all of them on games with spreads between
# 36 and 51.5 points. It is the most emphatic "unbettable" a feed can say --
# more so than -8000, which at least has a number -- and reading it as missing
# data was this module's worst bug: it promoted those games to the TOP of the
# filtered board, which is precisely backwards.
MONEYLINE_OFF = "OFF"


def _odds_raw(node, side, phase="close"):
    """The raw string in one cell of ESPN's nested moneyline block.

    The shape is moneyline -> {home,away} -> {open,close} -> odds, and `odds`
    is a STRING ('-180', '+150', or 'OFF'). `close` on an unplayed game means
    "latest", not "final"; the last capture before kickoff is what makes it a
    closing line, which is why build_game_entities keeps re-reading it and
    generate_insights never overwrites a stored price with None."""
    try:
        return ((node or {}).get(side) or {}).get(phase, {}).get("odds")
    except AttributeError:
        return None


def _odds_int(node, side, phase="close"):
    """One signed integer out of that cell, or None for OFF/absent/garbage."""
    try:
        return int(float(str(_odds_raw(node, side, phase)).replace("+", "")))
    except (TypeError, ValueError):
        return None


def parse_event(event):
    """One scoreboard event -> its price block, or None when the feed carries
    no usable moneyline for it.

    Both sides are stored, never just the picked one: which side a game's lean
    falls on can change between runs, and a store holding only "the price of
    the side we liked on Tuesday" cannot be re-read honestly on Saturday."""
    try:
        comp = (event.get("competitions") or [{}])[0]
    except (AttributeError, IndexError):
        return None
    books = comp.get("odds") or []
    if not books:
        # NOTHING KNOWN -- genuinely no market published, which on a college
        # Saturday is mostly the FBS-vs-FCS games. Distinct from a market the
        # book has pulled; see MONEYLINE_OFF.
        return None
    book = books[0]
    ml = book.get("moneyline") or {}
    home, away = _odds_int(ml, "home"), _odds_int(ml, "away")
    off = any(str(_odds_raw(ml, side)).strip().upper() == MONEYLINE_OFF
              for side in ("home", "away"))
    if home is None or away is None:
        # Half a price is worse than none -- it would silently settle one side
        # at a real number and the other at nothing -- so neither is kept. The
        # block itself still is: its SPREAD is what says how lopsided the game
        # was, and `moneyline_off` is what tells the filter this is a refusal
        # to quote rather than an absence of data.
        home = away = None
    spread = book.get("spread")
    return {
        "provider": ((book.get("provider") or {}).get("name")
                     or (book.get("provider") or {}).get("displayName")),
        "home_ml": home,
        "away_ml": away,
        "moneyline_off": off,
        "spread": float(spread) if isinstance(spread, (int, float)) else None,
        "details": book.get("details"),
        "captured_at": datetime.datetime.now(datetime.timezone.utc)
                               .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def fetch_moneylines(session, sport, dates):
    """{espn_event_id: price block} across `dates` (YYYY-MM-DD strings).

    NEVER RAISES. A price is a nicety layered onto a slate that is already
    built and already scoreable; a book outage, a shape change or a 500 must
    cost the run nothing at all. Failures are counted and reported by the
    caller rather than swallowed in silence -- the one thing worse than no
    odds is no odds and no mention of it.
    """
    league = LEAGUE_PATHS.get(sport)
    if not league:
        return {}
    url = _SCOREBOARD.format(league)
    out = {}
    for day in sorted({str(d)[:10] for d in dates if d}):
        params = {"dates": day.replace("-", ""), "limit": 400}
        if sport == "cfb":
            params["groups"] = FBS_GROUPS
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            events = resp.json().get("events") or []
        except Exception:  # noqa: BLE001 -- see the docstring; never fatal
            continue
        for event in events:
            priced = parse_event(event)
            if priced:
                out[str(event.get("id"))] = priced
    return out


def for_side(odds, side_abbr, home_abbr, away_abbr):
    """The American price for the side a pick actually took, or None.

    Matches on the LEADING TOKEN of `side`, the same rule web/insights' team
    tinting uses: EPL's double_chance sides read 'ARS or Draw', so an equality
    test against the abbr would silently price nothing on that market."""
    if not odds or not side_abbr:
        return None
    lead = str(side_abbr).split()[0]
    if lead == home_abbr:
        return odds.get("home_ml")
    if lead == away_abbr:
        return odds.get("away_ml")
    return None


def attach(session, sport, entities, dates, espn_ids=None):
    """Hang an `odds` block on every entity the feed has a price for, and say
    out loud how many that was.

    THE TWO SPORTS KEY THEIR STORES DIFFERENTLY, which is the only thing
    `espn_ids` exists for. CFB's store is keyed by whatever `game_id` its
    schedule carried, and cfbfastR shares ESPN's id space (see
    cfb_grading.fetch_slate), so its keys ARE ESPN event ids and it passes
    nothing. NFL's are nflverse ids like '2026_01_CLE_JAX', so it passes
    {entity_key: espn_id} read off games.csv's own `espn` column -- the same
    join nfl_grading.py already does to grade at all.

    An entity whose key finds no price simply has no `odds` key, which every
    reader treats as "unpriced" rather than as an error.

    Returns the number priced. Never raises -- see fetch_moneylines."""
    if not entities:
        return 0
    priced = fetch_moneylines(session, sport, dates)
    espn_ids = espn_ids or {}
    hit = 0
    for key, ent in entities.items():
        block = priced.get(str(espn_ids.get(key, key)))
        if block:
            ent["odds"] = block
            hit += 1
    leaned = sum(1 for e in entities.values() if (e.get("standout") or {}).get("score"))
    unpriced_leans = sum(1 for e in entities.values()
                         if (e.get("standout") or {}).get("score") and not e.get("odds"))
    print("insights(games): {} odds -- {} of {} games priced ({} of {} leans unpriced). "
          "ESPN drops the line at final, so this is capture-before-kickoff only."
          .format(sport, hit, len(entities), unpriced_leans, leaned))
    return hit


# ------------------------------------------------------------------------- #
# The bettability filter.
# ------------------------------------------------------------------------- #

def unbettable(price_american, cap):
    """Whether a price is past the point where this model could ever clear it.

    Returns (True, break_even) when it is, (False, break_even) otherwise, and
    (False, None) for a price that does not exist -- UNPRICED IS NOT
    UNBETTABLE, and conflating the two is the failure mode worth naming. ESPN
    carries no line at all for a chunk of every board (17 of 71 events on
    2026-09-19, mostly the smaller programs), and dropping those picks would
    silently delete the part of the slate least likely to be efficiently
    priced -- the opposite of what a bettability filter is for. No price means
    unknown; the pick stands and says so."""
    be = break_even(price_american)
    if be is None or not cap:
        return False, be
    return be > cap, be


def apply_bettability(entities, cap, sport=""):
    """Strip the PICK from any game priced past `cap`, leaving the opinion.

    WHAT GETS REMOVED IS THE BET, NOT THE ANALYSIS. `standout` and
    `best_angle` go, so the game stops being presented as something to back
    and stops reaching the ledger. `betting_signals` and `signal_scores` stay
    exactly as they were: the model still thinks Notre Dame beats Michigan
    State, that is still worth showing, and hiding it would be pretending the
    model said nothing when it said something unbackable. The card renders the
    reason in place of the pick.

    WHY IT IS NEEDED. A Signal Score cannot see price, so the top of a college
    board is systematically the least bettable part of it: on the 2026-09-19
    slate the model's three highest-scored picks were quoted -4000, -8000 and
    -3200, needing 98%, 99% and 97%. Its best-ever measured band hit 90.9%
    (10-1 at scores of 90+). Everything above that line is a pick it has never
    demonstrated it can clear, presented at the top of the page as its most
    confident work.

    THIS CHANGES THE RECORD, deliberately and visibly. From here the graded
    CFB record counts bettable picks only, so it will read lower than the
    77.3% that came before it and mean considerably more -- 48% of those 44
    picks were games decided by 21+ points. The discontinuity is the point,
    not a side effect.

    Returns the number of picks suppressed."""
    if not cap:
        return 0
    cut = 0
    for ent in (entities or {}).values():
        standout = ent.get("standout")
        if not standout:
            continue
        odds = ent.get("odds") or {}
        american = for_side(odds, standout.get("side"),
                            (ent.get("home") or {}).get("abbr"),
                            (ent.get("away") or {}).get("abbr"))
        bad, be = unbettable(american, cap)
        if odds.get("moneyline_off"):
            # THE MOST EMPHATIC UNBETTABLE THERE IS. The book has pulled the
            # market rather than quote it -- on the 2026-09-19 board every one
            # of these sat behind a spread of 36 to 51.5 points. Checked before
            # the price test because there is no price to test.
            ent["no_bet"] = {
                "reason": "off_the_board",
                "american": None,
                "display": "OFF",
                "break_even": None,
                "break_even_display": None,
                "cap_display": "{:.0f}%".format(100.0 * cap),
                "spread": odds.get("details"),
                "score": standout.get("score"),
                "side": standout.get("side"),
            }
            ent["standout"] = None
            ent["best_angle"] = None
            cut += 1
            continue
        if not bad or american is None:
            # `american is None` cannot happen while unbettable() treats a
            # missing price as bettable, and is checked anyway: the formatter
            # below does int(american), so any future change to that rule would
            # turn a policy decision into a TypeError mid-slate. Belt and
            # braces on a path that runs unattended four times a day.
            continue
        ent["no_bet"] = {
            "reason": "price",
            "american": american,
            "display": "{:+d}".format(int(american)),
            "break_even": round(be, 4),
            "break_even_display": "{:.0f}%".format(100.0 * be),
            "cap_display": "{:.0f}%".format(100.0 * cap),
            "spread": odds.get("details"),
            "score": standout.get("score"),
            "side": standout.get("side"),
        }
        ent["standout"] = None
        ent["best_angle"] = None
        cut += 1
    if cut:
        print("insights(games): {} bettability -- {} pick{} suppressed for price "
              "(needs more than {:.0%}; the model's best measured band is 90.9%). "
              "Their Signal Scores still render; they are no longer bets."
              .format(sport, cut, "" if cut == 1 else "s", cap))
    return cut
