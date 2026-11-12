"""The boost-ticket log: what the Bets tab would have named each morning, and
what happened to it.

    python3 boost_log.py --settle --capture     # the daily workflow, after regenerate
    python3 boost_log.py --report               # read-only

WHY THIS EXISTS (2026-11-12). The Bets tab names tickets "worth +24.6% per $1".
That figure is arithmetic on the book's prices: each leg's chance is its
two-sided price with the vig removed, and the legs are multiplied as if
independent. Neither step has ever been checked against results on tickets the
page would actually name, and CLAUDE.md has carried "predicted vs realised is
not tracked" since the tray shipped. This is the record that lets it be
checked: a ticket's stated chance against how often such tickets cash, and its
stated value against what they returned.

WHAT IS LOGGED. The reader's own promo lives in his browser and never reaches
the pipeline, so the log cannot record what he saw. It records REFERENCE
tickets instead: once a day, for each promo in REFERENCE_PROMOS, the two
tickets `parlay_boost.suggest` names from that morning's board (best value,
best Kelly growth). The promos are the owner's own description of the terms he
is offered (2026-11-10): three or more legs at +300, sometimes +400, sometimes
with a per-leg floor of -200 or -250, sometimes only +100 on two legs -- all
at a 50% boost. They are a measurement grid. They are NOT a default promo and
nothing on the page is pre-filled from them.

  * The boost size chooses which ticket is named, but the thing measured --
    did it cash as often as the market's chance said -- does not depend on it.
  * A ticket's value is reported at its own promo's boost.

THE FILE, data/boost_tickets.jsonl, is APPEND-ONLY (invariant 6):

  ticket rows  written ONCE per (slate_date, promo, kind): the first capture
               of the day wins, the repo's rule for every pre-game snapshot.
               Only legs whose start is a known instant still in the future
               can be on one, so a ticket is never logged after any of its
               games began.
  result rows  written once per ticket, when every leg's game is decided.
               won / lost, or "reduced" when a leg was called off or tied: a
               book drops that leg and re-prices the rest, the boost may no
               longer qualify, and the stated chance was for a different
               ticket. Reduced tickets are counted and kept out of both
               comparisons.

Nothing here reaches a Signal Score, a weight or the graded ledger, and no
price reaches a model (invariant 1): it reads the display board and public
results.
"""

import argparse
import datetime
import json
import os
import random
import sys

import parlay_boost
import slate_clock

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "boost_tickets.jsonl")
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "data.json")
SCHEMA = 1

# The measurement grid: the owner's words, 2026-11-10 ("3+ legs +300 odds
# minimum ... sometimes +400 ... minimum -200 per leg sometimes -250 ...
# sometimes minimum +100 odds total parlay"), at the 50% boost he gave as the
# example. Ids are part of the write-once key: never reuse one for new terms.
REFERENCE_PROMOS = [
    ("b50-3leg-300", {"boost_pct": 50, "min_legs": 3, "min_total_american": 300}),
    ("b50-3leg-400", {"boost_pct": 50, "min_legs": 3, "min_total_american": 400}),
    ("b50-3leg-300-floor200", {"boost_pct": 50, "min_legs": 3, "min_total_american": 300,
                               "leg_floor_american": -200}),
    ("b50-3leg-300-floor250", {"boost_pct": 50, "min_legs": 3, "min_total_american": 300,
                               "leg_floor_american": -250}),
    ("b50-2leg-100", {"boost_pct": 50, "min_legs": 2, "min_total_american": 100}),
]
KINDS = ("value", "growth")
BOOTSTRAP_ITERS = 4000
BOOTSTRAP_SEED = 17      # the repo's seed, so a re-run prints the same interval


# ------------------------------------------------------------------ the file
def load(path=LOG_PATH):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append(rows, path=LOG_PATH):
    """Append rows; never rewrites what is there."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def ticket_key(row):
    return (row["slate_date"], row["promo_id"], row["kind"])


# ------------------------------------------------------------------- capture
def board_legs(board, now):
    """The board's rows as suggest() legs: priced both ways, and starting at a
    KNOWN instant still in the future. A row with no start instant is left
    out here (the page keeps it, because hiding an undated bet would blank the
    board; a log cannot, because it could not say the ticket was pre-game)."""
    legs = []
    for row in (board.get("straight") or []) + (board.get("parlay") or []):
        start = row.get("start_utc")
        if not start or row.get("fair") is None or row.get("decimal") is None:
            continue
        when = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        if when <= now:
            continue
        # Which side of the game the leg is on decides how it settles, so a
        # row whose side is neither of its two clubs (never observed) is left
        # out rather than settled on a guess.
        if row.get("side") not in (row.get("home"), row.get("away")) or row.get("home") == row.get("away"):
            continue
        home = row.get("home")
        legs.append({
            "game": "{}:{}".format(row.get("sport"), row.get("id")),
            "label": "{} {}".format(row.get("side"), row.get("display")),
            "decimal": row["decimal"], "fair": row["fair"],
            "sport": row.get("sport"), "id": str(row.get("id")), "side": row.get("side"),
            "side_is_home": row.get("side") == home,
            "american": row.get("american"), "start_utc": start,
            "game_date": slate_clock.eastern_date(when),
        })
    return legs


def capture(board, existing, now=None, slate_date=None):
    """New ticket rows for today's board: for each reference promo, the value
    and growth tickets, unless that (day, promo, kind) is already logged."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    slate = slate_date or str(slate_clock.eastern_date(now))
    have = {ticket_key(r) for r in existing if r.get("type") == "ticket"}
    legs = board_legs(board or {}, now)
    rows = []
    for promo_id, promo in REFERENCE_PROMOS:
        named = parlay_boost.suggest(legs, promo)
        for kind in KINDS:
            ticket = named.get(kind)
            # A ticket not worth its stake is not one the page recommends, so
            # it is not one whose promise needs checking.
            if not ticket or ticket["ev_boosted"] <= 0:
                continue
            if (slate, promo_id, kind) in have:
                continue
            rows.append({
                "type": "ticket", "schema": SCHEMA,
                "captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "slate_date": slate, "promo_id": promo_id, "promo": promo, "kind": kind,
                "legs": [{k: leg[k] for k in ("sport", "id", "side", "side_is_home", "american",
                                               "decimal", "fair", "start_utc", "game_date")}
                         for leg in ticket["legs"]],
                "win_prob": ticket["win_prob"], "decimal": ticket["decimal"],
                "boosted_american": ticket["boosted_american"],
                "ev_boosted": ticket["ev_boosted"], "kelly": ticket["kelly"],
            })
    return rows


# -------------------------------------------------------------------- settle
def leg_result(leg, facts, called_off):
    """won / lost / void for one leg from its game's observed facts, or None
    while the game is undecided. A tie voids the leg (a moneyline push)."""
    if called_off:
        return "void"
    if not facts:
        return None
    result = facts.get("result")
    if result == "T":
        return "void"
    if result not in ("H", "A"):
        return None
    return "won" if (result == "H") == bool(leg["side_is_home"]) else "lost"


def adapter_resolver(session, config):
    """resolve(leg) -> won / lost / void / None, reading each (sport, Eastern
    date) slate once through the same adapters the grader uses, so a leg is
    settled by exactly the rule its sport's picks are."""
    import signal_report
    slates = {}

    def resolve(leg):
        adapter = signal_report.SPORT_ADAPTERS.get(leg["sport"])
        if adapter is None:
            return None
        key = (leg["sport"], leg["game_date"])
        if key not in slates:
            slates[key] = adapter["fetch_slate"](session, config, leg["game_date"])
        event = slates[key].get(str(leg["id"]))
        if event is None:
            return None
        return leg_result(leg, adapter["observed_facts"](event), adapter["is_called_off"](event))

    return resolve


def settle(rows, resolve, today=None, now=None):
    """Result rows for every logged ticket not yet settled whose legs are all
    decided. `resolve(leg)` gives won / lost / void / None.

    A ticket is looked at only once its last game's Eastern date is before
    `today`: the daily run is in the morning, and asking about tonight's games
    would be a request per sport for an answer that cannot exist yet. One
    undecided leg leaves the whole ticket for the next run -- EXCEPT that a
    lost leg settles it at once, since no later result can change a loss."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = today or str(slate_clock.eastern_date(now))
    done = {tuple(r["key"]) for r in rows if r.get("type") == "result"}
    out = []
    for row in rows:
        if row.get("type") != "ticket" or ticket_key(row) in done:
            continue
        if max(leg["game_date"] for leg in row["legs"]) >= today:
            continue
        results = [resolve(leg) for leg in row["legs"]]
        if "lost" in results:
            outcome = "lost"
        elif None in results:
            continue
        elif "void" in results:
            outcome = "reduced"
        else:
            outcome = "won"
        boost = float(row["promo"]["boost_pct"]) / 100.0
        out.append({
            "type": "result", "schema": SCHEMA, "key": list(ticket_key(row)),
            "settled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "outcome": outcome,
            "legs": [{"sport": leg["sport"], "id": leg["id"], "result": res}
                     for leg, res in zip(row["legs"], results)],
            # Profit per $1 staked at this ticket's own boost; None when the
            # ticket was re-priced by a void leg and this log does not know how.
            "return_per_dollar": ((row["decimal"] - 1.0) * (1.0 + boost) if outcome == "won"
                                  else -1.0 if outcome == "lost" else None),
        })
    return out


# -------------------------------------------------------------------- report
def _interval(values, iters=BOOTSTRAP_ITERS, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def summary(rows, kind=None):
    """Predicted against realised over settled won/lost tickets.

    {"logged", "settled", "reduced", "cashed", "cash_rate", "expected_rate",
     "rate_gap", "rate_gap_ci", "roi", "expected_roi", "roi_gap", "roi_gap_ci"}
    with None for every figure there is no settled ticket behind. The gaps are
    realised minus predicted, per ticket, so their intervals are paired.

    THE SAME TICKET IS COUNTED ONCE. Five promos and two kinds read one board,
    and the best-value ticket is usually the same three legs under several of
    them (all five on 2026-11-12). Counted per row, one Saturday would enter
    the record five times. So tickets are de-duplicated by day and leg set;
    the first row logged stands for it.

    DIFFERENT TICKETS FROM ONE DAY STILL SHARE LEGS, so they are not
    independent draws and these intervals are too narrow by an amount that
    depends on the overlap. Read them as a floor on the uncertainty."""
    tickets, seen = {}, set()
    for r in rows:
        if r.get("type") != "ticket" or (kind is not None and r["kind"] != kind):
            continue
        same = (r["slate_date"], tuple(sorted((l["sport"], str(l["id"]), l["side"]) for l in r["legs"])))
        if same in seen:
            continue
        seen.add(same)
        tickets[ticket_key(r)] = r
    results = {tuple(r["key"]): r for r in rows if r.get("type") == "result"}
    pairs = [(tickets[k], results[k]) for k in tickets if k in results]
    graded = [(t, r) for t, r in pairs if r["outcome"] in ("won", "lost")]
    out = {"logged": len(tickets), "settled": len(graded),
           "reduced": sum(1 for _, r in pairs if r["outcome"] == "reduced"),
           "cashed": sum(1 for _, r in graded if r["outcome"] == "won"),
           "cash_rate": None, "expected_rate": None, "rate_gap": None, "rate_gap_ci": None,
           "roi": None, "expected_roi": None, "roi_gap": None, "roi_gap_ci": None}
    if not graded:
        return out
    n = len(graded)
    hit = [1.0 if r["outcome"] == "won" else 0.0 for _, r in graded]
    rate_gap = [h - t["win_prob"] for h, (t, _) in zip(hit, graded)]
    roi_gap = [r["return_per_dollar"] - t["ev_boosted"] for t, r in graded]
    out.update({
        "cash_rate": sum(hit) / n,
        "expected_rate": sum(t["win_prob"] for t, _ in graded) / n,
        "rate_gap": sum(rate_gap) / n,
        "roi": sum(r["return_per_dollar"] for _, r in graded) / n,
        "expected_roi": sum(t["ev_boosted"] for t, _ in graded) / n,
        "roi_gap": sum(roi_gap) / n,
    })
    if n >= 2:
        out["rate_gap_ci"] = list(_interval(rate_gap))
        out["roi_gap_ci"] = list(_interval(roi_gap))
    return out


def report(rows):
    lines = []
    for label, kind in (("all tickets", None), ("best value", "value"), ("best growth", "growth")):
        s = summary(rows, kind)
        if not s["settled"]:
            lines.append("{:<12} {} logged, none settled yet".format(label, s["logged"]))
            continue
        ci = s["rate_gap_ci"]
        lines.append(
            "{:<12} {} settled of {} logged ({} reduced): cashed {:.1%} against {:.1%} stated, "
            "gap {:+.1%}{}; returned {:+.1%} per $1 against {:+.1%} stated".format(
                label, s["settled"], s["logged"], s["reduced"], s["cash_rate"], s["expected_rate"],
                s["rate_gap"], " [{:+.1%}, {:+.1%}]".format(*ci) if ci else "",
                s["roi"], s["expected_roi"]))
    lines.append("Each distinct ticket is counted once. Tickets from one day share legs, so the intervals are too narrow; "
                 "a boosted ticket cashes rarely, so the return needs hundreds of them.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--settle", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--data", default=DATA_PATH, help="the built payload (output/data.json)")
    args = parser.parse_args(argv)
    rows = load()
    if args.settle:
        import requests
        import yaml
        with open(os.path.join(os.path.dirname(LOG_PATH), "..", "config.yaml")) as fh:
            config = yaml.safe_load(fh)
        # A feed that is down must not cost today's capture: settling can wait
        # a day, the morning board cannot be captured tomorrow.
        try:
            new = settle(rows, adapter_resolver(requests.Session(), config))
        except Exception as exc:  # noqa: BLE001 -- reported, retried next run
            print("boost log: settling FAILED ({}: {}); tickets stay open and are retried "
                  "next run".format(type(exc).__name__, str(exc)[:200]))
            if os.environ.get("GITHUB_ACTIONS"):
                print("::warning title=Boost tickets not settled::{}".format(str(exc)[:200]))
        else:
            append(new)
            rows += new
            print("boost log: settled {} ticket{}".format(len(new), "" if len(new) == 1 else "s"))
    if args.capture:
        with open(args.data) as fh:
            board = ((json.load(fh).get("insights") or {}).get("bets")) or {}
        new = capture(board, rows)
        append(new)
        rows += new
        print("boost log: captured {} ticket{} from {} dated priced legs".format(
            len(new), "" if len(new) == 1 else "s",
            len(board_legs(board, datetime.datetime.now(datetime.timezone.utc)))))
    if args.report or not (args.capture or args.settle):
        print(report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
