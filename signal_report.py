#!/usr/bin/env python3
"""How did yesterday's top Signal Score picks actually do?

A standalone, read-only reporting CLI. It reads the picks that
`betting_signals.py` already generated (out of the committed
data/insights.games.json store) and grades them against real outcomes from the
MLB StatsAPI. It does NOT change, re-run, or re-derive how picks are made:
betting_signals and implied_total are imported read-only (only for ranking) or
not at all.

    python3 signal_report.py                      # yesterday (US/Eastern)
    python3 signal_report.py --date 2026-07-22
    python3 signal_report.py --date 2026-07-21 --rev f27b478

The pick set
------------
One pick per game: the game's `standout` market -- the highest Signal Score that
clears `betting_signals.mlb.standout_threshold` (50) -- ranked by score. This is
the same selection `betting_signals.top_market()` makes and the same one the UI
ranks games by, so the report and the site never disagree about what the pick
was. `--all-markets` widens to every market carrying a lean at or above
`--min-score` instead (noisier: run_line shares moneyline's exact config weights,
so it duplicates that row at the same score on most games).

Rows are keyed by gamePk, never by matchup string: split doubleheaders put the
same matchup on the slate twice (PIT@NYY and BAL@BOS both appear twice on
2026-07-22), and a matchup key would collide.

Two records, never one number
-----------------------------
There are no betting lines anywhere in this project -- an explicit, documented
deferral (docs/mlb-availability-field-map.md, docs/sports-pulse-schema.md). What
a pick can be graded against therefore differs by market, and the report keeps
the resulting records SEPARATE rather than blending them into one percentage:

  * OUTCOME-GRADED -- moneyline, first-five moneyline, first-inning runs. These
    resolve from the box score against what actually happened in the world.

  * ESTIMATE-GRADED -- game total, first-five total, team total, when the store
    carries the run estimate. `_attach_estimates` (fetchers/mlb.py) mutates the
    standout in place with implied_total's `point`/`low`/`high`, and
    generate_insights writes that standout to the store, so the number displayed
    beside the pick is recoverable. Grading a total against it asks "did the lean
    agree with the model's own point estimate, against reality" -- a calibration
    check on our own number, NOT evidence of a predictive edge against a market.
    That is a weaker claim than an outcome-graded row makes, so it gets its own
    tally.

A total whose store predates the estimate work (data/insights.games.json at
73615c8, the 2026-07-22 slate, was committed ten commits before it) carries no
number and stays UNPRICED, listed with its actual runs but in neither record.
Run lines are UNPRICED permanently: implied_total produces totals, and a run line
is a margin. `--all-markets` rows other than the standout are UNPRICED too, since
the store keeps `betting_signals` and `standout` but not `signal_scores`.

`--assume-lines` additionally grades what is left against conventional round
numbers (8.5 / 4.5 / 4.5 / -1.5). Those are invented, so they form a THIRD tally
and never touch the other two. It is opt-in and labeled in the header.
"""

import argparse
import datetime
import json
import subprocess
import sys

import requests
import yaml

import betting_signals  # read-only: ranking (list_markets / top_market)

CONFIG_PATH = "config.yaml"
STORE_PATH = "data/insights.games.json"
SPORT_KEY = "mlb"

# Reference lines used ONLY under --assume-lines. These are conventional round
# numbers, not real market lines, and not derived from anything in this repo.
ASSUMED_LINES = {"game_total": 8.5, "team_total": 4.5, "first_five_total": 4.5}
ASSUMED_RUN_LINE = 1.5  # picked side assumed to be laying it (win by 2+)

# Markets that cannot resolve without a line (see module docstring).
UNPRICED_MARKETS = ("game_total", "first_five_total", "team_total", "run_line")

# Narrower labels than insights_ui's, to keep a row inside a terminal width.
SHORT_LABELS = {"first_five_moneyline": "First Five ML", "nrfi_yrfi": "First-Inning Runs"}

# Verdicts that count toward a record. Everything else is listed and explained,
# but never folded into a hit rate.
GRADED = ("HIT", "MISS")
# Verdicts that a later re-run could still turn into a real result.
RERUNNABLE = ("PENDING",)

# What a graded row was measured against. These are kept in separate records:
# "beat reality" and "agreed with our own estimate" are different claims, and one
# blended percentage would quietly average them. Order = display order.
BASES = ("outcome", "estimate", "assumed")
BASIS_LABEL = {"outcome": "Outcome-graded", "estimate": "Estimate-graded",
               "assumed": "Assumed-line-graded"}
BASIS_SUFFIX = {"outcome": "", "estimate": " (est)", "assumed": " (asm)"}

EXIT_OK, EXIT_PENDING, EXIT_USAGE = 0, 1, 2


# --------------------------------------------------------------------------- #
# inputs: config, store, schedule
# --------------------------------------------------------------------------- #

def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def load_store(path, rev=None):
    """The insights game store, from the working tree or from a git revision.

    `--rev` matters because the store is pruned to a single slate on every
    generate_insights run and the deploy workflow never commits it (it holds
    `permissions: contents: read`), so any date but the most recently committed
    one lives only in git history.
    """
    if rev:
        spec = "{}:{}".format(rev, path)
        try:
            raw = subprocess.run(["git", "show", spec], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        except subprocess.CalledProcessError as e:
            lines = e.stderr.decode().strip().splitlines()
            die("cannot read {} ({})".format(spec, lines[-1] if lines else "git show failed"))
        return json.loads(raw)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        die("no store at {}".format(path))


def store_slate_dates(store):
    """The date(s) the store's own `generated_at` stamps point at. A slate is
    generated the morning of its games, so this is the slate date -- but it is a
    hint, verified against the schedule before anything is graded."""
    return sorted({(e.get("generated_at") or "")[:10] for e in store.values() if e.get("generated_at")})


def fetch_slate(session, base_url, date):
    """{gamePk: game} for one date, with linescores (per-inning runs)."""
    r = session.get("{}/schedule".format(base_url),
                    params={"sportId": 1, "date": date, "hydrate": "linescore"}, timeout=30)
    r.raise_for_status()
    out = {}
    for day in r.json().get("dates", []):
        for g in day.get("games", []):
            out[str(g.get("gamePk"))] = g
    return out


def fetch_replay_dates(session, base_url, pks):
    """{gamePk: date} for the completed version of a postponed game, which keeps
    its gamePk and reappears on the date it was actually played."""
    if not pks:
        return {}
    r = session.get("{}/schedule".format(base_url),
                    params={"sportId": 1, "gamePks": ",".join(sorted(pks)), "hydrate": "linescore"},
                    timeout=30)
    r.raise_for_status()
    out = {}
    for day in r.json().get("dates", []):
        for g in day.get("games", []):
            if is_final(g):
                out[str(g.get("gamePk"))] = day.get("date")
    return out


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def is_final(g):
    """A game that actually finished.

    Deliberately NOT `abstractGameState == "Final"`: a POSTPONED game also
    reports abstract state "Final" (codedGameState "D", detailedState
    "Postponed") with null scores, so the abstract state alone would grade a
    rainout as a played game. "O" is Game Over -- final, pre-official.
    """
    st = g.get("status") or {}
    if st.get("codedGameState") not in ("F", "O"):
        return False
    teams = g.get("teams") or {}
    return all((teams.get(s) or {}).get("score") is not None for s in ("away", "home"))


def is_called_off(g):
    """Postponed or cancelled -- never played on this date."""
    st = g.get("status") or {}
    return st.get("codedGameState") in ("D", "C") or \
        (st.get("detailedState") or "") in ("Postponed", "Cancelled")


def live_state(g):
    """A short human status for a game that has not finished."""
    st = g.get("status") or {}
    detailed = st.get("detailedState") or "Unknown"
    ls = g.get("linescore") or {}
    if st.get("abstractGameState") == "Live" and ls.get("currentInningOrdinal"):
        return "{} — {} {}".format(detailed, (ls.get("inningState") or "").strip(),
                                   ls.get("currentInningOrdinal")).replace("  ", " ")
    if detailed == "Scheduled" and g.get("gameDate"):
        return "Scheduled (first pitch {}Z)".format(g["gameDate"][11:16])
    return detailed


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #

def innings_runs(g, upto=None):
    """(away, home) runs summed over the first `upto` innings (all if None), and
    how many innings of linescore data were actually available."""
    inn = ((g.get("linescore") or {}).get("innings") or [])
    window = inn if upto is None else inn[:upto]
    away = sum((i.get("away") or {}).get("runs") or 0 for i in window)
    home = sum((i.get("home") or {}).get("runs") or 0 for i in window)
    return away, home, len(inn)


def _side_verdict(pick, winner):
    if winner is None:
        return "PUSH"
    return "HIT" if pick == winner else "MISS"


def grade(pick, game, assume_lines):
    """(result_text, verdict, basis) for one pick against one game record.

    Verdicts: HIT / MISS (count toward a record) · PUSH (no side landed) ·
    UNPRICED (needs a number that does not exist) · PENDING (not resolved yet, a
    re-run can settle it) · POSTPONED (not played on this date) · UNRESOLVED
    (final, but this market cannot be read off it -- e.g. a game called before
    the 5th for a first-five pick).

    `basis` is what a graded row was measured against -- "outcome", "estimate",
    or "assumed" -- and None for everything else. Callers tally the three
    separately; see BASES.
    """
    bt, side = pick["bet_type"], pick["side"]
    away, home = pick["away_abbr"], pick["home_abbr"]

    if game is None:
        return "not on this date's schedule", "UNRESOLVED", None
    if is_called_off(game):
        st = game.get("status") or {}
        reason = st.get("reason") or st.get("detailedState") or "Postponed"
        text = "{} ({})".format(st.get("detailedState") or "Postponed", reason) \
            if reason != st.get("detailedState") else (st.get("detailedState") or "Postponed")
        if pick.get("replayed_on"):
            # Month-day only: the year is already in the report header, and the
            # full ISO date pushes this row past the RESULT column.
            text += " → played {}".format(pick["replayed_on"][5:])
        return text, "POSTPONED", None
    if not is_final(game):
        return live_state(game), "PENDING", None

    # A pick's side must name something this game can be read against. If it does
    # not, say so -- never fall through to a comparison that would score a
    # malformed side as a clean MISS.
    if bt in ("moneyline", "first_five_moneyline", "run_line") and side not in (away, home):
        return "side {!r} is neither team".format(side), "UNRESOLVED", None
    if bt == "nrfi_yrfi" and side not in ("NRFI", "YRFI"):
        return "side {!r} is not NRFI/YRFI".format(side), "UNRESOLVED", None
    if bt == "team_total" and side.split(" ")[0] not in (away, home):
        return "side {!r} names no team".format(side), "UNRESOLVED", None
    if bt in ("game_total", "first_five_total", "team_total") and side.split(" ")[-1] not in ("Over", "Under"):
        return "side {!r} is neither Over nor Under".format(side), "UNRESOLVED", None

    teams = game.get("teams") or {}
    a_score = (teams.get("away") or {}).get("score")
    h_score = (teams.get("home") or {}).get("score")
    final = "{} {} - {} {}".format(away, a_score, home, h_score)

    # ---- outcome-graded: settled by what happened, no number needed ----------
    if bt == "moneyline":
        winner = away if a_score > h_score else home if h_score > a_score else None
        return ((final + " (tie)") if winner is None else final,
                _side_verdict(side, winner), "outcome")

    if bt == "first_five_moneyline":
        f_away, f_home, n_inn = innings_runs(game, 5)
        if n_inn < 5:
            return "only {} inning(s) played".format(n_inn), "UNRESOLVED", None
        text = "{} {} - {} {} thru 5".format(away, f_away, home, f_home)
        if f_away == f_home:
            return text + " (tie)", "PUSH", "outcome"
        return text, _side_verdict(side, away if f_away > f_home else home), "outcome"

    if bt == "nrfi_yrfi":
        f_away, f_home, n_inn = innings_runs(game, 1)
        if n_inn < 1:
            return "no linescore data", "UNRESOLVED", None
        runs = f_away + f_home
        text = "{} run{} in the 1st".format(runs, "" if runs == 1 else "s")
        return text, ("HIT" if (side == "YRFI") == (runs > 0) else "MISS"), "outcome"

    # ---- needs a number -----------------------------------------------------
    # Run lines never get one: implied_total produces totals, and a run line is a
    # margin. Only --assume-lines can grade it, on an invented number.
    if bt == "run_line":
        margin = (a_score - h_score) if side == away else (h_score - a_score)
        if not assume_lines:
            return "{} (margin {:+d})".format(final, margin), "UNPRICED", None
        # Compact form: the full score plus "(margin +3)" plus the line runs past
        # the RESULT column and would be truncated mid-number.
        return ("{} · {:+d} vs -{}".format(final, margin, ASSUMED_RUN_LINE),
                ("HIT" if margin >= 2 else "MISS"), "assumed")

    if bt in ("game_total", "first_five_total", "team_total"):
        if bt == "game_total":
            actual, text = a_score + h_score, "{} total runs".format(a_score + h_score)
        elif bt == "first_five_total":
            f_away, f_home, n_inn = innings_runs(game, 5)
            if n_inn < 5:
                return "only {} inning(s) played".format(n_inn), "UNRESOLVED", None
            actual, text = f_away + f_home, "{} runs thru 5".format(f_away + f_home)
        else:
            abbr = side.split(" ")[0]
            actual = a_score if abbr == away else h_score
            text = "{} scored {}".format(abbr, actual)
        over = side.split(" ")[-1] == "Over"

        # The run estimate the pick was actually displayed with, carried on the
        # stored standout by _attach_estimates. It is a whole number, so landing
        # exactly on it is a genuine tie -- a PUSH, not a silent win for Under.
        point = pick.get("point")
        if point is not None:
            text = "{} vs est {}".format(text, point)
            if actual == point:
                return text + " (tie)", "PUSH", "estimate"
            return text, ("HIT" if (actual > point) == over else "MISS"), "estimate"

        if not assume_lines:
            return text, "UNPRICED", None
        line = ASSUMED_LINES[bt]
        return ("{} vs {}".format(text, line),
                ("HIT" if (actual > line) == over else "MISS"), "assumed")

    return "unknown market", "UNRESOLVED", None


# --------------------------------------------------------------------------- #
# pick selection
# --------------------------------------------------------------------------- #

def _stored_standout(entry, min_score):
    """The store's own `standout`, when it is the pick we are being asked for.

    Preferred over recomputing from `betting_signals`, for two reasons. It is
    what the site actually displayed -- if a store was written by a different
    build of the generator, that record wins over anything we would re-derive
    now. And it is the ONLY place the run estimate survives: _attach_estimates
    mutates this dict with point/low/high, while the `betting_signals` block it
    was derived from never carries them, so recomputing silently drops the number
    a total needs to be gradeable.

    `standout` is the highest-scoring market that cleared the generation-time
    threshold, so it answers `--min-score` too -- unless the floor was raised
    above its score, in which case nothing on this game qualifies.
    """
    sd = entry.get("standout")
    if not sd or not sd.get("bet_type") or sd.get("score") is None:
        return None
    return sd if sd["score"] >= min_score else None


def collect_picks(store, config, min_score, all_markets):
    """The day's picks, ranked by Signal Score desc (gamePk as a deterministic
    tiebreak). Ranking comes from betting_signals so this never diverges from
    what the site showed."""
    labels = dict((config.get("insights_ui") or {}).get(SPORT_KEY, {}).get("market_labels") or {})
    labels.update(SHORT_LABELS)
    picks = []
    for pk, entry in store.items():
        scored = entry.get("betting_signals") or {}
        stored = _stored_standout(entry, min_score)
        if all_markets:
            markets = [m for m in betting_signals.list_markets(scored) if m["score"] >= min_score]
            # Only the standout carries an estimate (the store keeps no
            # signal_scores), so merge it onto its own row and leave the rest
            # unpriced rather than pretending every row has a number.
            if stored:
                for m in markets:
                    if m["bet_type"] == stored["bet_type"] and m["side"] == stored["side"]:
                        m.update({k: stored[k] for k in ("point", "low", "high", "unit")
                                  if k in stored})
        elif stored:
            markets = [stored]
        else:
            top = betting_signals.top_market(scored, min_score)
            markets = [top] if top else []
        for m in markets:
            picks.append({
                "point": m.get("point"),
                "gamePk": pk,
                "away_abbr": (entry.get("away") or {}).get("abbr"),
                "home_abbr": (entry.get("home") or {}).get("abbr"),
                "start": entry.get("start"),
                "bet_type": m["bet_type"],
                "market": labels.get(m["bet_type"], m["bet_type"]),
                "side": m["side"],
                "score": m["score"],
                "flags": m.get("flags") or [],
            })
    picks.sort(key=lambda p: (-p["score"], p["gamePk"]))
    return picks


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

ROW = "{:<5}  {:<17} {:<8} {:<17} {:<11} {:<31} {:<10}{}"
RESULT_WIDTH = 31


def short_start(start):
    return (start or "").replace(" PM ET", "p").replace(" AM ET", "a")


def fit(text, width=RESULT_WIDTH):
    """Keep a long status string from pushing the VERDICT column out of
    alignment. Every result string is written to fit; this is the backstop for
    an unanticipated one (a novel detailedState, a long postponement reason)."""
    text = text or ""
    return text if len(text) <= width else text[:width - 1] + "…"


def matchup(pick, game):
    label = "{} @ {}".format(pick["away_abbr"], pick["home_abbr"])
    if game and game.get("doubleHeader") in ("S", "Y") and game.get("gameNumber"):
        label += " (g{})".format(game["gameNumber"])
    return label


def render(date, picks, rows, store_size, assume_lines, out=sys.stdout):
    def w(line=""):
        out.write(line.rstrip() + "\n")

    w()
    w("Signal Score picks — {}   ({} pick{} from {} game{})".format(
        date, len(picks), "" if len(picks) == 1 else "s",
        store_size, "" if store_size == 1 else "s"))
    if any(r[4] == "estimate" for r in rows):
        w("Rows marked (est) are graded against the run estimate shown beside the pick, not a market line:")
        w("that is a calibration check on our own number, so it is tallied separately from outcome-graded rows.")
    if assume_lines:
        w("--assume-lines ON: rows marked (asm) use ASSUMED reference numbers "
          "(game {} / team {} / F5 {} / run line -{}).".format(
              ASSUMED_LINES["game_total"], ASSUMED_LINES["team_total"],
              ASSUMED_LINES["first_five_total"], ASSUMED_RUN_LINE))
        w("Those are conventional round numbers, NOT real market lines — no line data exists in this project.")
    w()
    w(ROW.format("SCORE", "MATCHUP", "START", "MARKET", "PICK", "RESULT", "VERDICT", ""))
    for pick, game, result, verdict, basis in rows:
        flags = "  ⚑ " + ",".join(pick["flags"]) if pick["flags"] else ""
        w(ROW.format(pick["score"], matchup(pick, game), short_start(pick["start"]),
                     pick["market"], pick["side"], fit(result),
                     verdict + BASIS_SUFFIX.get(basis, ""), flags))
    w()
    for line in summary_lines(date, rows):
        w(line)


def summary_lines(date, rows):
    """The day's records -- one per grading basis, never combined.

    An outcome-graded row says the pick matched what happened; an estimate-graded
    row says it matched our own point estimate. Averaging those into a single
    percentage would state a stronger claim than the estimate rows support, so
    each basis keeps its own record and the non-graded verdicts are counted
    beside them.
    """
    records = {b: {"HIT": 0, "MISS": 0, "PUSH": 0} for b in BASES}
    other = {}
    for _, _, _, verdict, basis in rows:
        if basis in records and verdict in records[basis]:
            records[basis][verdict] += 1
        else:
            other[verdict] = other.get(verdict, 0) + 1

    parts = []
    for basis in BASES:
        rec = records[basis]
        n = rec["HIT"] + rec["MISS"]
        if not n and not rec["PUSH"]:
            continue
        # A basis can hold nothing but pushes (an estimate landed exactly on the
        # actual). Report the pushes without an empty 0-0 (n/a) record.
        seg = "{} {}-{}{} on {} pick{}".format(
            BASIS_LABEL[basis], rec["HIT"], rec["MISS"],
            " ({:.0f}%)".format(100.0 * rec["HIT"] / n) if n else "",
            n, "" if n == 1 else "s")
        if rec["PUSH"]:
            seg += " [{} push]".format(rec["PUSH"])
        parts.append(seg)
    parts.extend("{} {}".format(v, k.lower()) for k, v in sorted(other.items()))
    parts.append("{} total".format(len(rows)))

    lines = ["Summary  {}:  {}".format(date, "  ·  ".join(parts))] if parts else []
    pending = sum(1 for r in rows if r[3] in RERUNNABLE)
    if pending:
        lines.append("         {} pick{} unresolved — re-run after they finish.".format(
            pending, "" if pending == 1 else "s"))
    return lines


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def die(msg, code=EXIT_USAGE):
    sys.stderr.write("signal_report: {}\n".format(msg))
    sys.exit(code)


def yesterday_et():
    """Yesterday in US/Eastern -- the slate boundary that matches the site's
    displayed start times."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # no tzdata: EDT-ish fallback, only shifts the default date
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
    return (now.date() - datetime.timedelta(days=1)).isoformat()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Report how a date's top Signal Score picks actually did (read-only).")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                   help="slate date to report on (default: yesterday, US/Eastern)")
    p.add_argument("--store", default=STORE_PATH, help="insights game store path")
    p.add_argument("--rev", default=None, metavar="GIT_REV",
                   help="read the store from a git revision (the store only ever holds one slate)")
    p.add_argument("--min-score", type=int, default=None, metavar="N",
                   help="Signal Score floor (default: betting_signals.<sport>.standout_threshold)")
    p.add_argument("--all-markets", action="store_true",
                   help="every market with a lean at/above the floor, not one standout per game")
    p.add_argument("--assume-lines", action="store_true",
                   help="grade totals/run lines against ASSUMED reference numbers (opt-in, labeled)")
    args = p.parse_args(argv)
    if args.date:
        try:
            datetime.date.fromisoformat(args.date)
        except ValueError:
            die("--date must be YYYY-MM-DD, got {!r}".format(args.date))
    else:
        args.date = yesterday_et()
    return args


def main(argv=None):
    args = parse_args(argv)
    config = load_config()
    base_url = config[SPORT_KEY]["base_url"]
    threshold = (config.get("betting_signals") or {}).get(SPORT_KEY, {}).get("standout_threshold", 50)
    min_score = args.min_score if args.min_score is not None else threshold

    store = load_store(args.store, args.rev)
    if not store:
        die("store {} is empty".format(args.store))

    session = requests.Session()
    try:
        slate = fetch_slate(session, base_url, args.date)
    except requests.RequestException as e:
        die("schedule fetch failed for {}: {}".format(args.date, e))

    # Verify the store actually covers the requested date before grading anything
    # against it. A postponed game still appears on its original date, so a store
    # pk that is absent from the slate means a genuine store/date mismatch.
    overlap = [pk for pk in store if pk in slate]
    if not overlap:
        stamps = store_slate_dates(store) or ["unknown"]
        die("store {}{} covers {} ({} games), not {} — no gamePk overlap.\n"
            "  Try: --date {}   or   --rev <commit whose store covers {}>\n"
            "  (the store is pruned to one slate per run; past slates live in git history)".format(
                args.store, " @ " + args.rev if args.rev else "",
                "/".join(stamps), len(store), args.date, stamps[0], args.date))

    picks = collect_picks(store, config, min_score, args.all_markets)
    if len(overlap) < len(store):
        sys.stderr.write("signal_report: warning: {} of {} stored games are not on the {} "
                         "schedule; their picks are listed as UNRESOLVED\n".format(
                             len(store) - len(overlap), len(store), args.date))

    # Postponed picks: find where (if anywhere) the game was actually played, so
    # the row can say so instead of just "Postponed".
    called_off = {p["gamePk"] for p in picks
                  if p["gamePk"] in slate and is_called_off(slate[p["gamePk"]])}
    replays = {}
    if called_off:
        try:
            replays = fetch_replay_dates(session, base_url, called_off)
        except requests.RequestException:
            replays = {}  # best-effort; the row still reports the postponement

    rows = []
    for pick in picks:
        game = slate.get(pick["gamePk"])
        pick["replayed_on"] = replays.get(pick["gamePk"])
        result, verdict, basis = grade(pick, game, args.assume_lines)
        rows.append((pick, game, result, verdict, basis))

    render(args.date, picks, rows, len(store), args.assume_lines)
    return EXIT_PENDING if any(r[3] in RERUNNABLE for r in rows) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
