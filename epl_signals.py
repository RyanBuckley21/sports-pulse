"""EPL Betting Signal Layer -- deterministic per-match Signal Scores.

Scores TWO markets from ONE lean -- double_chance and match_result (3-way
moneyline). They are not two models: identical weights, identical score, and
the only thing separating them is the bar each must clear, because a draw wins
one and loses the other. See _MARKET_PRECEDENCE and config's
market_thresholds. The generic scoring math comes from signal_core.py, shared with
nfl_signals.py and cfb_signals.py; what lives here is the sport wiring:
SIGNAL_SPECS, _base_signals, list_markets, score_game, outcome_split and
build_inputs.

Config-driven and sport-keyed like every other sport:
config["betting_signals"]["epl"] holds the weights, scales and thresholds. They
are BACKTEST-DERIVED, not placeholders -- see epl_backtest.py and the epl block
in config.yaml for the measurement and its caveats.

THE ONE THING THAT DOES NOT PORT FROM THE OTHER SPORTS: DRAWS.

MLB, NFL and CFB all score a two-outcome market. Ties are ~0.2% of NFL games,
and nfl_backtest simply excludes them from its reliability pass ("no direction
to correlate a tie against"). A draw is not that. It is a normal result in this
sport -- 23.6% of the 2,660 matches measured here, more than one in five -- and
every part of this module has to treat it as an outcome rather than as an
absence of one. Three places that shows up:

  * ONE MARKET COUNTS IT, THE OTHER PAYS FOR IT, AND THE DRAW RATE DECIDES
    WHICH IS OFFERED. double_chance picks "HOME or Draw", so a draw wins it;
    match_result picks the side outright, so a draw loses it. Scoring only the
    first would be safe but would ignore that double chance is routinely priced
    near its own break-even (1.21 at the 55 bar), which is a poor proposition
    however high the hit rate reads. Scoring only the second would make a draw
    a pure 23.6% tax on every pick.

    So match_result is admitted only above a higher bar, set where the draw tax
    is MEASURABLY below the league rate: at score >= 75 it is 18.2%, 95% CI
    [14.4%, 22.6%], whose upper bound clears the 23.3% base. At 70 the CI still
    overlaps and there is no measured edge to justify taking the worse side of
    a draw. Above that bar the outright market takes precedence and becomes the
    standout; below it, only double chance is offered. The escalation is the
    draw probability doing work, not a preference.

  * THE RELIABILITY PASS KEEPS IT. Correlation is against HOME GOAL
    DIFFERENCE, a continuous outcome that uses every match and is monotone in
    the 3-way result, rather than a point-biserial against home_win with draws
    dropped. epl_backtest.py reports the home_win version alongside for
    comparability with the NFL and CFB numbers; they agree closely, which is
    what makes this a better estimator rather than a different question.

  * THE OUTPUT NAMES IT, AND PRICES IT. outcome_split() returns the measured
    probability of all three results for a given Signal Score, and every market
    carries the win_prob and fair_odds derived from it -- so a draw is visible
    as a real outcome with a real frequency (13-27% across bands) AND as the
    break-even each market has to beat. A hit rate alone cannot tell you
    whether a pick is worth taking; a break-even next to the price can.

WHAT IS *NOT* DONE, because the data will not support it: the model does not
PICK draws, and nothing here pretends it can. That was tested rather than
assumed. A draw is never the modal outcome in any lean band; correlation with
the lean is -0.013 (95% CI [-0.051, +0.027], spans zero) and with the match's
scoring environment -0.016 (CI [-0.057, +0.023], spans zero) -- the latter
tested specifically because a draw might plausibly depend on a SUM (how
low-scoring both sides are) rather than the difference signal_core.paired
computes, and it does not. In every lean band, picking the leaning side beats
picking the draw, by 6 to 33 points. A draw band would be a fabricated signal.

THAT NEAR-ZERO CORRELATION IS LINEAR, AND THE RELATIONSHIP IS NOT. Draws sit
flat at 25.4% through the 50-69 band and 24.8% through 70-84, then fall to
13.9% at 85+ -- a difference of -11.5pp with 95% CI [-18.1, -4.5], which
excludes zero. Both facts are true and each drives a different decision: the
flat middle is why a draw band near zero lean fails, and the tail is why an
outright market at a high bar works. Reading only the linear r would lose the
second one.

NO HOME-ADVANTAGE INTERCEPT, and this is a real architectural limit rather than
an oversight. signal_core.paired() is a pure home-minus-away difference and
raw_lean() averages those differences, so two equally-formed sides produce a
lean of exactly zero -- while the home side in fact wins 43.4% of such matches
and the away side 32.9%. A constant term cannot be fitted into that shape: it
has no variance to correlate and no stdev to scale by (it divides by zero).
`venue_form` is what carries venue information instead, comparing the home
side's home record against the away side's away record, and it measurably does
some of that job -- adding it moves the share of home picks from 50.0% to
55.5%. The residual cost was measured rather than assumed: sweeping a decision
offset over the lean recovers at most +0.3pp of accuracy, because the picks a
home bias would flip sit near zero lean and are close to coin flips anyway.
Flagged, small, and not silently assumed away.

No availability override. There is no pregame "expected XI" feed in the ESPN
data this repo uses, so unlike MLB's scratched-probable and NFL's QB-out rules
there is nothing to key one on -- the same position CFB is in, and handled the
same way: nothing here pretends to know about team news.
"""

import signal_core
from signal_core import (coerce as _coerce, finalize as _finalize,
                         paired as _paired, raw_lean as _raw_lean)


# Every candidate signal's definition -- which per-side input key feeds it,
# which config scale normalizes it, and which direction favors home. This is
# the SINGLE place that mapping lives: _base_signals reads it to build the
# live, config-scaled, tanh-squashed lean, and epl_backtest.py's reliability
# pass reads the SAME table to build the raw, unscaled gap it correlates
# against real outcomes -- so a signal's definition cannot drift between what
# score_game computes and what was measured to calibrate it.
#
# Every candidate is listed, INCLUDING the four that carry no weight, for
# exactly that reason: the backtest measures what is in this table, and a
# signal that was measured and then rejected has to stay measurable or the
# rejection cannot be re-checked on new data. Which ones are weighted is
# config's business, not this table's -- see config.yaml's epl block for why
# each of goal_diff, form_ppm, recent_gd and rest ended up at zero.
#
# Order is display/iteration order only; it carries no weighting meaning.
SIGNAL_SPECS = {
    "attack": {"home_key": "home_gf_pm", "away_key": "away_gf_pm",
               "scale_key": "attack_gap", "favors": "higher"},
    "defense": {"home_key": "home_ga_pm", "away_key": "away_ga_pm",
                "scale_key": "defense_gap", "favors": "lower"},
    "recent_form": {"home_key": "home_recent_ppm", "away_key": "away_recent_ppm",
                    "scale_key": "recent_form_gap", "favors": "higher"},
    "venue_form": {"home_key": "home_venue_ppm", "away_key": "away_venue_ppm",
                   "scale_key": "venue_form_gap", "favors": "higher"},
    # Measured, then rejected. Kept measurable -- see the note above.
    "form_ppm": {"home_key": "home_ppm", "away_key": "away_ppm",
                 "scale_key": "form_ppm_gap", "favors": "higher"},
    "goal_diff": {"home_key": "home_gd_pm", "away_key": "away_gd_pm",
                  "scale_key": "goal_diff_gap", "favors": "higher"},
    "recent_gd": {"home_key": "home_recent_gd_pm", "away_key": "away_recent_gd_pm",
                  "scale_key": "recent_gd_gap", "favors": "higher"},
    "rest": {"home_key": "home_rest", "away_key": "away_rest",
             "scale_key": "rest_gap", "favors": "higher"},
}

# Minimum prior matches THIS SEASON each side needs before its form means
# anything. Form never carries across a season boundary -- promotion and
# relegation turn over three clubs a year, so last season's table is a
# different league -- which makes every August a cold start.
#
# 5 is measured, not picked for roundness: out-of-sample accuracy is flat
# across gates (51.5% at 0, 51.4% at 5, 52.1% at 10), so a higher gate buys
# almost nothing and costs coverage. What the gate is really protecting
# against is the small-denominator problem min_games was reclassified for on
# the leaderboards: one match played makes goals-per-match a 90-minute
# sample, and a 4-0 opening day would read as the best attack in the league.
MIN_MATCHES = 5


def _base_signals(inp, scales):
    """Every base signal's directional value toward HOME, or None where inputs
    are missing. Iterates SIGNAL_SPECS rather than hardcoding each extraction
    -- see that table's docstring for why."""
    return {name: _paired(inp.get(spec["home_key"]), inp.get(spec["away_key"]),
                          scales[spec["scale_key"]], spec["favors"])
            for name, spec in SIGNAL_SPECS.items()}


# How each market labels its two sides. double_chance is not "home vs away" --
# both of its sides CONTAIN the draw, which is the point of scoring it, so the
# labels have to say so. Kept as a table rather than inlined because the second
# market this sport gets will need its own and should not have to edit
# score_game to say so.
#
# "ARS or Draw" survives the UI's team-colour lookup unchanged: insights.js
# resolves a side to a colour on its LEADING token, so the chip is still
# Arsenal red rather than the neutral gold.
def _labels_for(bet_type, home, away):
    if bet_type == "double_chance":
        return ("%s or Draw" % home, "%s or Draw" % away)
    return (home, away)  # match_result: the side outright, a draw loses


def score_game(config, sport_key, inputs):
    """Score every configured bet type for one match (v1: double_chance only).
    Returns {bet_type: {side, score, flags}}. Empty dict if the sport isn't
    configured, or if either side is short of MIN_MATCHES -- an unscored match
    is the honest output for a cold start, not a zero-confidence lean.

    No `availability` parameter, unlike betting_signals.score_game and
    nfl_signals.score_game: there is no team-news feed here to key one on.
    See the module docstring."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    bet_types = cfg.get("bet_types") or {}
    if not bet_types:
        return {}
    if (inputs.get("home_played") or 0) < MIN_MATCHES or (inputs.get("away_played") or 0) < MIN_MATCHES:
        return {}
    scales = cfg["scales"]
    min_t = cfg.get("min_threshold", 55)
    per_market = cfg.get("market_thresholds") or {}

    sig = _base_signals(inputs, scales)
    labels = (inputs.get("home_abbr"), inputs.get("away_abbr"))
    out = {}
    for bt, weights in bet_types.items():
        w = dict(weights)
        bt_sig = {k: sig.get(k) for k in w}
        L, n, agree = _raw_lean(bt_sig, w)
        # PER-MARKET THRESHOLD, and this is the whole mechanism by which the
        # draw gets priced into an outright pick rather than merely reported.
        #
        # Both markets read the SAME lean -- there is one model here, not two.
        # What differs is the bar each has to clear, because a draw costs them
        # opposite things: it wins double_chance and loses match_result. So the
        # outright market is admitted only where the draw tax is measurably
        # small, which is a property of the score band and was measured (see
        # config.yaml's market_thresholds). Below that bar match_result simply
        # does not qualify and only the safe market is offered.
        entry = _finalize(L, n, agree, per_market.get(bt, min_t), _labels_for(bt, *labels))
        split = outcome_split(config, sport_key, entry.get("score", 0))
        if split:
            entry["win_prob"] = _win_prob(bt, split)
            entry["fair_odds"] = round(1.0 / entry["win_prob"], 2) if entry["win_prob"] else None
        out[bt] = entry
    return out


def _win_prob(bet_type, split):
    """The share of outcomes this market WINS on, from the measured split.
    double_chance takes the side outright plus the draw; match_result takes the
    side only. Rounded to 4dp so a config table of 3dp inputs cannot produce a
    spuriously precise probability."""
    if bet_type == "double_chance":
        return round(split["side"] + split["draw"], 4)
    return round(split["side"], 4)


def outcome_split(config, sport_key, score):
    """The measured chance of each of the three results, for a pick carrying
    `score`. Returns {side, draw, other} summing to 1.0, or None when the sport
    has no table configured.

    THIS IS WHERE THE DRAW BECOMES VISIBLE. The market already counts a draw as
    a win, but a reader still deserves to see how the three results actually
    split -- a 56.4% / 26.2% / 17.4% match reads very differently from a 65.6%
    / 19.8% / 14.6% one even though both are the same pick. Without this the
    draw is invisible on the card right up until it decides the match.

    NOT A MODEL. These are observed frequencies from real completed matches,
    banded by Signal Score -- the same deterministic, rules-based footing as
    everything else here, with no fitted distribution in sight. `side` is the
    leaning side winning outright, `draw` is a draw, `other` is the opposite
    side winning; for double_chance the pick wins on side+draw, which is why
    those two are reported apart rather than pre-added.

    Bands are chosen by `min_score`, taking the highest band the score clears,
    so the table is extended by adding a row rather than by touching code."""
    cfg = (config.get("betting_signals") or {}).get(sport_key) or {}
    table = cfg.get("outcome_split") or []
    chosen = None
    for row in table:
        if score >= row.get("min_score", 0):
            if chosen is None or row["min_score"] >= chosen["min_score"]:
                chosen = row
    if not chosen:
        return None
    return {"side": chosen["side"], "draw": chosen["draw"], "other": chosen["other"]}


# Precedence for tie-breaking the standout when two markets share the top score
# -- and here they ALWAYS do, which makes this table load-bearing rather than a
# formality. Both markets are scored from the same lean, so both carry the
# identical Signal Score; what separates them is only which one qualified.
#
# match_result FIRST is the deliberate part. Its threshold is the higher of the
# two, so it qualifies only on the matches where the draw tax was measured to be
# small -- exactly the matches where taking the outright side is the better
# proposition and double chance is priced worst. So the standout escalates by
# itself: the safe market below that bar, the outright one above it, with no
# separate rule deciding when to switch.
_MARKET_PRECEDENCE = ("match_result", "double_chance")


def list_markets(scored):
    """Every market carrying a real lean (side != 'No clear lean'), as
    [{bet_type, side, score, flags}] sorted by Signal Score desc. Mirrors
    nfl_signals.list_markets -- no per-side expansion, EPL v1 having no
    team_total-style market."""
    prec = {k: i for i, k in enumerate(_MARKET_PRECEDENCE)}
    candidates = []
    for bt, entry in (scored or {}).items():
        if not isinstance(entry, dict):
            continue
        side = entry.get("side")
        if side and side != "No clear lean":
            candidates.append((bt, side, entry.get("score", 0), entry.get("flags") or []))
    candidates.sort(key=lambda c: (-c[2], prec.get(c[0], 99)))
    return [{"bet_type": bt, "side": side, "score": score, "flags": list(flags)}
            for bt, side, score, flags in candidates]


def top_market(scored, threshold):
    """Delegates to signal_core.top_market, passing THIS module's ranking in.
    See signal_core.top_market for why the shared helper takes an already-
    ranked list rather than the raw `scored` dict."""
    return signal_core.top_market(list_markets(scored), threshold)


def build_inputs(away_abbr, home_abbr, away_played, home_played,
                 away_gf_pm, home_gf_pm, away_ga_pm, home_ga_pm,
                 away_ppm, home_ppm, away_gd_pm, home_gd_pm,
                 away_recent_ppm, home_recent_ppm,
                 away_recent_gd_pm, home_recent_gd_pm,
                 away_venue_ppm, home_venue_ppm,
                 away_rest, home_rest):
    """Assemble the deterministic input dict from already-computed per-side
    form values, mirroring nfl_signals.build_inputs' role.

    `*_played` are NOT signals -- they are the cold-start gate score_game
    checks before scoring anything at all."""
    return {
        "away_abbr": away_abbr, "home_abbr": home_abbr,
        "away_played": _coerce(away_played), "home_played": _coerce(home_played),
        "away_gf_pm": _coerce(away_gf_pm), "home_gf_pm": _coerce(home_gf_pm),
        "away_ga_pm": _coerce(away_ga_pm), "home_ga_pm": _coerce(home_ga_pm),
        "away_ppm": _coerce(away_ppm), "home_ppm": _coerce(home_ppm),
        "away_gd_pm": _coerce(away_gd_pm), "home_gd_pm": _coerce(home_gd_pm),
        "away_recent_ppm": _coerce(away_recent_ppm), "home_recent_ppm": _coerce(home_recent_ppm),
        "away_recent_gd_pm": _coerce(away_recent_gd_pm), "home_recent_gd_pm": _coerce(home_recent_gd_pm),
        "away_venue_ppm": _coerce(away_venue_ppm), "home_venue_ppm": _coerce(home_venue_ppm),
        "away_rest": _coerce(away_rest), "home_rest": _coerce(home_rest),
    }
