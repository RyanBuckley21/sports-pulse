"""The generic Signal Score math, shared by every sport's scoring module.

Extracted from betting_signals.py (MLB), nfl_signals.py and cfb_signals.py,
which each carried their own copy. The three copies were verified identical
before extraction -- not by eye but mechanically: AST-normalised (docstrings
and comments stripped) hashes matched across all three for coerce, round,
paired, raw_lean and top_market, and the bodies were then cross-executed
against each other over exhaustive input grids. See the PR for the evidence.

WHAT LIVES HERE: the pipeline a lean goes through, and nothing above it --
parse a number, squash a home-vs-away gap through tanh, combine the
available signals into one weighted lean, turn that lean into a side and a
score. All of it is sport-agnostic by construction: nothing here knows what
a bullpen or a quarterback is.

WHAT DELIBERATELY DOES NOT LIVE HERE, so this module stays a calculator
rather than a framework:

  * `_base_signals` -- which raw inputs feed which named signal. MLB
    hand-builds seven (including a season-series ratio computed inline and
    three combined totals); NFL and CFB iterate a SIGNAL_SPECS table. That
    is sport wiring, not shared math.
  * `list_markets` -- ~80% common, but MLB expands `team_total` into
    per-side away/home candidates and the others have no such market.
    Factoring it out means a flag or a hook in shared code serving exactly
    one caller, which is how a shared module starts to rot. Three near-copies
    of twelve lines is the cheaper problem.
  * `score_game` / `build_inputs` -- entirely sport-specific by nature.
  * Every MLB-only helper (run_line guard, team_total, probable-pitcher
    availability flags) stays in betting_signals.py.

NOTHING IMPORTS THIS YET. It is added on its own so the extraction can be
reviewed as a pure addition with no behavioural risk, before any sport
module is migrated onto it.
"""

import math


def coerce(v):
    """Parse a numeric input (float, int, or display string like ".812"/"4.02")
    to float, or None if absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def round_half_up(x):
    """Round half UP (not banker's rounding) so scores match the hand-mocked
    proposal values, e.g. 60.5 -> 61.

    Named round_half_up rather than the `_round` the sport modules used:
    inside a shared module that name shadows the builtin, and the behaviour
    genuinely differs from it (Python's round() is banker's rounding, so
    round(60.5) is 60, not 61). A reader who assumes the builtin here would
    be wrong by one score point on every exact half."""
    return int(math.floor(x + 0.5))


def paired(val_home, val_away, scale, favors):
    """Directional value toward HOME (+1) from a home-vs-away gap, tanh-squashed
    by `scale`. favors='higher' -> a higher home value leans home; favors='lower'
    -> a lower home value leans home. None if either side is missing."""
    if val_home is None or val_away is None:
        return None
    d = math.tanh((val_home - val_away) / scale)
    return d if favors == "higher" else -d


def raw_lean(sig, weights):
    """Weighted net lean L in [-1, 1] over the available (non-None) signals,
    renormalized by their weights. A signal present with value 0 (e.g. an even
    series) still counts toward the weight sum -- it's a real neutral input, not
    a missing one. Returns (L, n_available, n_agreeing) or (None, 0, 0)."""
    pairs = [(sig.get(k), w) for k, w in weights.items() if sig.get(k) is not None]
    if not pairs:
        return None, 0, 0
    wsum = sum(w for _, w in pairs)
    if wsum <= 0:
        return None, 0, 0
    L = sum(d * w for d, w in pairs) / wsum
    agree = sum(1 for d, _ in pairs if abs(d) > 1e-9 and (d > 0) == (L > 0))
    return L, len(pairs), agree


def finalize(L, n_avail, n_agree, threshold, labels, flags=(), force_aligned=False):
    """Turn a net lean into {side, score, flags}. 'No clear lean' when the score
    is under threshold, or (for multi-signal bets) fewer than 2 signals agree
    with the net direction. `force_aligned` bypasses the alignment guard when an
    exogenous availability penalty has been applied.

    `force_aligned` is used by TWO sports, not one: MLB applies it when a
    probable starter is scratched, NFL when a starting quarterback is
    Out/Doubtful. CFB has no injury feed and so never passes it -- its own
    copy of this function omitted the parameter entirely. Defaulting to False
    makes adopting this version a behavioural no-op for CFB, verified
    exhaustively over 960 combinations of L / n_avail / n_agree / threshold
    with zero mismatches. The parameter is not vestigial either: True and
    False diverge in 140 of those same 960 cases."""
    flags = sorted(set(flags))
    if L is None:
        return {"side": "No clear lean", "score": 0, "flags": flags}
    score = round_half_up(100 * min(1.0, abs(L)))
    aligned = True if (force_aligned or n_avail < 2) else (n_agree >= 2)
    if score >= threshold and aligned:
        side = labels[0] if L >= 0 else labels[1]
    else:
        side = "No clear lean"
    return {"side": side, "score": score, "flags": flags}


def top_market(ranked_markets, threshold):
    """Deterministically pick a game's single most-notable market: the first
    entry of an ALREADY-RANKED market list that clears `threshold`. Returns
    {bet_type, side, score, flags} or None when nothing clears the bar.

    TAKES THE RANKED LIST, not the raw `scored` dict -- and that is a real
    correction to the three copies this replaces, not a cosmetic one. Each
    sport module's version began `for m in list_markets(scored)`, textually
    identical in all three and therefore hash-identical, but `list_markets`
    resolves from the CALLING MODULE's globals and the three list_markets
    implementations are NOT the same (MLB expands team_total into per-side
    candidates; NFL and CFB do not). Lifting the old body into a shared module
    verbatim would have bound it to whichever list_markets happened to live
    here -- silently wrong for at least two sports.

    Callers therefore pass their own ranking in:

        signal_core.top_market(list_markets(scored), threshold)

    which keeps each sport's market-expansion rules where they belong and
    leaves this function with the only genuinely shared part: "first one over
    the bar wins"."""
    for m in ranked_markets or []:
        if m.get("score", 0) >= threshold:
            return dict(m)
    return None
