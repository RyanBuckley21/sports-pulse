"""MLB training-data capture -- append-only feature/outcome stores.

Phase 1 of the predictive-model initiative (see
docs/mlb-training-data-phase-1-plan.md). Deterministic data capture ONLY: no
model, no training, no inference, no UI. Nothing here reads or changes
betting_signals.py or implied_total.py -- it passes through the input dict
`betting_signals.build_inputs()` already returns, and adds provenance.

Two append-only JSONL stores, joined on (gamePk, date):

  data/training/mlb_features.jsonl -- one PRE-GAME snapshot per game per day
  data/training/mlb_outcomes.jsonl -- one row per resolved game

The outcome row also carries the GROUND TRUTH for the one feature the pre-game
snapshot can only guess at: `actual_away_starter_id` / `actual_home_starter_id`,
the pitchers who really started. A feature row freezes the announced PROBABLE
before first pitch, and that probable is sometimes wrong -- measured against
real boxscores, 4 of the 162 captured probables in this store did not start
(~2.5%; n is small, so read it as "this happens", not as a rate). Joining the
two stores on (gamePk, date) therefore answers "was the probable we trained on
the pitcher who actually pitched", which is the difference between a model
learning from a guess and knowing it was a guess.

Recording the truth does NOT weaken the leakage invariant, because it lands on
the OUTCOME side. The feature row is still written once, pre-game, and never
touched; the starter id is written later, from the completed game, by the same
resolver that already writes the score. A reader who wants the pre-game view
alone can ignore the outcome store entirely, exactly as before.

Schema note: only rows resolved under SCHEMA_VERSION >= 2 carry these fields.
Historical rows lack the keys, which is an honest gap rather than a bug -- and a
recoverable one, since MLB rewrites probablePitcher to the real starter on a
finished game, so a backfill would cost one hydrated schedule call per
already-resolved date. Not built here.

CRITICAL INVARIANT -- neither store is ever rewritten. There is no "w"-mode
open and no rebuild-a-dict-then-save anywhere in this module; append is the
only write verb. That is the deliberate opposite of data/insights.games.json,
which is pruned back to today's slate on every run (see
generate_insights._generate_games) and of data/boxscores.json (pruned to
`touched` in mlb.build_game_entities). A feature row is written once, before
first pitch, and never touched again -- which is what makes label leakage
structurally impossible here rather than merely avoided by convention.

Leakage gates (all enforced in build_feature_row / build_outcome_row):
  * features are written only for a game that is BOTH still `Preview` AND
    still ahead of its posted first pitch, on the run's own date;
  * outcomes are written only for a game that is `Final` with a terminal
    status code and both scores present;
  * this module never computes a feature -- it cannot re-derive OPS, bullpen
    ERA, starter ERA or the season series, so a post-game value has no path
    into a feature row.
"""

import datetime
import json
import os

FEATURES_PATH = "data/training/mlb_features.jsonl"
OUTCOMES_PATH = "data/training/mlb_outcomes.jsonl"

# Bumped only when the row shape changes, so a reader can tell a month-1 row
# from a month-3 row instead of silently mis-parsing it.
#
# v2: outcome rows gain `actual_away_starter_id` / `actual_home_starter_id`.
# Feature rows are unchanged; only build_outcome_row emits the new fields, and
# only rows written from v2 onward carry them. Rows resolved before this bump
# simply lack the keys -- an honest gap, not a bug. A reader must therefore use
# .get() rather than [] and treat a missing key as "not recorded", which is a
# different statement from a present-but-null one ("MLB reported no starter").
SCHEMA_VERSION = 2

# MLB Stats API status vocabulary. `abstractGameState` is the coarse bucket
# (Preview / Live / Final); `statusCode` is the fine-grained one.
PREGAME_ABSTRACT = "Preview"
FINAL_ABSTRACT = "Final"
# F = Final, O = Game Over (terminal but not yet reconciled). Both are safe to
# label; anything else (S/P/PW pre-game, I/IR live, D*/postponed) is not.
FINAL_STATUS_CODES = frozenset({"F", "O"})
# Postponed/cancelled games never produce a result on this date. They get a
# marker row so the resolver stops retrying them and so the row is *visibly*
# excluded from training rather than silently blank forever.
VOID_DETAILED_STATES = frozenset({"Postponed", "Cancelled", "Canceled"})

# How far back the resolver looks for still-unlabelled games. Wide enough to
# self-heal a few missed runs; bounded so it never rescans the whole season.
DEFAULT_LOOKBACK_DAYS = 10


# --------------------------------------------------------------------------
# JSONL I/O -- append-only by construction.
# --------------------------------------------------------------------------

def _read_jsonl(path):
    """Every row in a JSONL store, or [] if it doesn't exist yet. Malformed
    lines are skipped rather than raising: a half-written line from an
    interrupted run must not make the whole history unreadable."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                print("training: skipping unparseable row in {}".format(path))
    return rows


def _append_jsonl(path, rows):
    """Append rows to a JSONL store. Mode "a" is the ONLY write mode used in
    this module -- see the module docstring's invariant."""
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


def _keys(rows):
    """The (gamePk, date) row keys present in a store. gamePk alone looks
    globally unique in the Stats API, but the pair is what actually matters:
    a postponed game replayed later can reappear under the same gamePk on a
    different officialDate, and the pair keeps those as distinct snapshots."""
    return {(r.get("gamePk"), r.get("date")) for r in rows}


# --------------------------------------------------------------------------
# Time helpers.
# --------------------------------------------------------------------------

def _parse_utc(ts):
    """Parse an MLB API UTC timestamp ("2026-07-26T16:15:00Z") to an aware
    datetime, or None if absent/unparseable. A None here fails the clock gate
    closed -- an unknown first pitch is never treated as 'not started yet'."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Feature capture (pre-game).
# --------------------------------------------------------------------------

def pregame_gate(game, game_date, now=None):
    """The two independent leakage gates, plus the date gate. Returns
    (ok, reason) -- `reason` names the failing gate for the skip log.

    Both gates must pass, not either, because each covers the other's blind
    spot: statusCode can linger at S/PW past the posted start during a delay,
    while a game can flip Preview -> Live midway through a long slate build.
    The clock gate is the hard one -- a plain timestamp comparison that does
    not depend on how MLB happens to be reporting state at that moment."""
    now = now or _now_utc()
    status = game.get("status") or {}

    # Date gate: only ever capture for the run's own date. Never a past date.
    official = game.get("officialDate")
    if official and official != game_date:
        return False, "date gate (officialDate {} != run date {})".format(official, game_date)

    # Status gate.
    abstract = status.get("abstractGameState")
    if abstract != PREGAME_ABSTRACT:
        return False, "status gate (abstractGameState={}, detailedState={})".format(
            abstract, status.get("detailedState"))

    # Clock gate.
    start = _parse_utc(game.get("gameDate"))
    if start is None:
        return False, "clock gate (no parseable gameDate)"
    if now >= start:
        return False, "clock gate (now {} >= first pitch {})".format(_iso(now), _iso(start))

    return True, None


def build_feature_row(game, signal_inputs, availability, game_date, now=None):
    """One pre-game feature row, or None if any leakage gate fails (the skip
    is logged, never written).

    `signal_inputs` is the dict betting_signals.build_inputs() returned, stored
    verbatim under "features". `availability` is the {away,home}_probable_out
    pair computed at the same callsite -- it is NOT part of build_inputs(), so
    it is captured alongside rather than by changing betting_signals."""
    now = now or _now_utc()
    ok, reason = pregame_gate(game, game_date, now=now)
    if not ok:
        print("training: SKIP gamePk {} -- {}".format(game.get("gamePk"), reason))
        return None

    status = game.get("status") or {}
    teams = game.get("teams") or {}
    away_pp = (teams.get("away") or {}).get("probablePitcher") or {}
    home_pp = (teams.get("home") or {}).get("probablePitcher") or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "gamePk": game.get("gamePk"),
        "date": game.get("officialDate") or game_date,
        "season": game.get("season"),
        "game_type": game.get("gameType"),
        "game_number": game.get("gameNumber"),
        "double_header": game.get("doubleHeader"),
        "start_utc": game.get("gameDate"),
        "venue": (game.get("venue") or {}).get("name"),
        "captured_at": _iso(now),
        "status_at_capture": {
            "abstract": status.get("abstractGameState"),
            "detailed": status.get("detailedState"),
            "code": status.get("statusCode"),
        },
        "features": dict(signal_inputs or {}),
        "availability": {
            "away_probable_out": bool((availability or {}).get("away_probable_out")),
            "home_probable_out": bool((availability or {}).get("home_probable_out")),
        },
        "probables": {"away_id": away_pp.get("id"), "home_id": home_pp.get("id")},
    }


def capture_features(rows, features_path=FEATURES_PATH):
    """Append feature rows, skipping any (gamePk, date) already on file.

    Skip-if-present, so the EARLIEST pre-game snapshot of a day wins: the
    pipeline can run more than once a day (workflow_dispatch, push to main,
    a local run), and a later re-run would only move the capture closer to
    first pitch. Returns (written, skipped)."""
    seen = _keys(_read_jsonl(features_path))
    fresh = []
    skipped = 0
    for row in rows or []:
        key = (row.get("gamePk"), row.get("date"))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)  # also dedupes within this batch
        fresh.append(row)
    written = _append_jsonl(features_path, fresh)
    return written, skipped


# --------------------------------------------------------------------------
# Outcome capture (post-game).
# --------------------------------------------------------------------------

def _inning_runs(innings, upto):
    """Combined runs across the first `upto` innings, or None if the game did
    not get that far or a half-inning's runs are missing (a walk-off leaves the
    home half of the last inning absent)."""
    if len(innings) < upto:
        return None
    total = 0
    for frame in innings[:upto]:
        for side in ("away", "home"):
            runs = (frame.get(side) or {}).get("runs")
            if runs is None:
                return None
            total += runs
    return total


def _actual_starter_id(teams, side):
    """The pitcher who actually started for `side`, or None.

    Read off `probablePitcher` on a FINAL game, which is not the same field it
    was pre-game: MLB REWRITES probablePitcher to the pitcher who really started
    once a game completes. Verified on the four games in this repo's own store
    whose captured probable did not start -- 823192, 823593, 824890 and 824000
    all now report the actual starter, 4 of 4.

    That is why recording this needs no boxscore call and no new capture step:
    the same schedule read the resolver already makes carries it, once the
    hydrate asks for it (see generate_insights.schedule_fetcher).

    Only ever call this on a game that has passed the completion gate below.
    Pre-game the identical field means the announced probable, which is exactly
    the value the feature row already froze -- reading it here would silently
    re-record the guess as though it were the answer.
    """
    return ((teams.get(side) or {}).get("probablePitcher") or {}).get("id")


def build_outcome_row(game, date_str, now=None):
    """One outcome row for a completed game, a void marker for a postponed or
    cancelled one, or None if the game has not resolved yet (in-progress,
    delayed, suspended) -- an unresolved game stays label-less and is retried
    on the next run, never guessed.

    A completed row also carries `actual_away_starter_id` /
    `actual_home_starter_id`: who really took the ball. The feature row froze a
    PROBABLE before first pitch, and that probable is sometimes wrong -- 4 of
    162 captured probables in this store did not start, including one
    (gamePk 823593) that was announced, scratched to null, and re-announced as
    a different pitcher, all pre-game. Storing the truth alongside the guess is
    what lets a reader filter or model those rows instead of training on them
    silently. n is small; treat 4/162 as a known limitation, not a rate.

    Null when MLB reports no probablePitcher on a finished game. Measured 0 of
    174 team-sides across the store's six dates, so this is unobserved rather
    than merely rare -- handled defensively because a postponed-then-resumed or
    forfeited game is a plausible case the sample does not contain.
    """
    now = now or _now_utc()
    status = game.get("status") or {}
    abstract = status.get("abstractGameState")
    detailed = status.get("detailedState")
    code = status.get("statusCode")

    if detailed in VOID_DETAILED_STATES:
        return {
            "schema_version": SCHEMA_VERSION,
            "gamePk": game.get("gamePk"),
            "date": date_str,
            "resolved_at": _iso(now),
            "resolution": "postponed",
            "final_status": {"abstract": abstract, "detailed": detailed, "code": code},
        }

    # Completion gate: coarse state, fine status code, and real scores must all
    # agree before anything is labelled.
    if abstract != FINAL_ABSTRACT or code not in FINAL_STATUS_CODES:
        return None
    teams = game.get("teams") or {}
    away_score = (teams.get("away") or {}).get("score")
    home_score = (teams.get("home") or {}).get("score")
    if away_score is None or home_score is None:
        return None

    linescore = game.get("linescore") or {}
    innings = linescore.get("innings") or []
    home_is_winner = (teams.get("home") or {}).get("isWinner")
    home_win = bool(home_is_winner) if home_is_winner is not None else home_score > away_score

    return {
        "schema_version": SCHEMA_VERSION,
        "gamePk": game.get("gamePk"),
        "date": date_str,
        "resolved_at": _iso(now),
        "final_status": {"abstract": abstract, "detailed": detailed, "code": code},
        "away_score": away_score,
        "home_score": home_score,
        "total_runs": away_score + home_score,
        "home_win": home_win,
        "innings_played": len(innings),
        "first_inning_runs": _inning_runs(innings, 1),
        "f5_total_runs": _inning_runs(innings, 5),
        # Ground truth for the probable each feature row froze -- see the
        # docstring and _actual_starter_id. Schema v2; absent on older rows.
        "actual_away_starter_id": _actual_starter_id(teams, "away"),
        "actual_home_starter_id": _actual_starter_id(teams, "home"),
    }


def pending_dates(today, lookback_days=DEFAULT_LOOKBACK_DAYS,
                  features_path=FEATURES_PATH, outcomes_path=OUTCOMES_PATH):
    """{date_str: {gamePk, ...}} for captured games that are past, still
    unlabelled, and inside the lookback window. Resolving every pending past
    date (not strictly yesterday) self-heals a missed run and picks up a
    postponed-then-replayed game for free, at no cost on a day with nothing
    outstanding."""
    resolved = _keys(_read_jsonl(outcomes_path))
    cutoff = today - datetime.timedelta(days=lookback_days)
    pending = {}
    for row in _read_jsonl(features_path):
        date_str = row.get("date")
        try:
            day = datetime.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            continue
        if not (cutoff <= day < today):
            continue
        if (row.get("gamePk"), date_str) in resolved:
            continue
        pending.setdefault(date_str, set()).add(row.get("gamePk"))
    return pending


def resolve_outcomes(fetch_schedule, today, lookback_days=DEFAULT_LOOKBACK_DAYS,
                     features_path=FEATURES_PATH, outcomes_path=OUTCOMES_PATH, now=None):
    """Label every resolvable pending game. `fetch_schedule(date_str)` returns
    the raw MLB schedule payload for that date (injected so this module holds
    no HTTP concerns of its own). Returns (labelled, voided, still_pending)."""
    pending = pending_dates(today, lookback_days, features_path, outcomes_path)
    if not pending:
        print("training(outcomes): nothing pending")
        return 0, 0, 0

    now = now or _now_utc()
    rows, voided, unresolved = [], 0, 0
    for date_str in sorted(pending):
        want = pending[date_str]
        try:
            sched = fetch_schedule(date_str)
        except Exception as e:  # noqa: BLE001 -- one bad date must not sink the rest
            print("training(outcomes): {} fetch failed ({}); retrying next run"
                  .format(date_str, str(e)[:120]))
            unresolved += len(want)
            continue
        by_pk = {g.get("gamePk"): g
                 for d in sched.get("dates", []) for g in d.get("games", [])}
        for pk in sorted(want, key=lambda x: (x is None, x)):
            game = by_pk.get(pk)
            if game is None:
                unresolved += 1
                continue
            row = build_outcome_row(game, date_str, now=now)
            if row is None:
                unresolved += 1
                continue
            if row.get("resolution") == "postponed":
                voided += 1
            rows.append(row)

    _append_jsonl(outcomes_path, rows)
    labelled = len(rows) - voided
    print("training(outcomes): labelled {}, voided {}, still pending {} (across {} date(s))"
          .format(labelled, voided, unresolved, len(pending)))
    return labelled, voided, unresolved
