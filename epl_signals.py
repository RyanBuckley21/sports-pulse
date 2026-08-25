"""EPL Betting Signal Layer -- deterministic per-match Signal Scores.

v1 scores ONLY match_result (1X2), the same "one market first" sequencing NFL
and CFB followed. The generic scoring math comes from signal_core.py, shared
with nfl_signals.py and cfb_signals.py; what lives here is the sport wiring:
SIGNAL_SPECS, _base_signals, list_markets, score_game and build_inputs.

Config-driven and sport-keyed like every other sport:
config["betting_signals"]["epl"] holds the weights, scales and thresholds. They
are BACKTEST-DERIVED, not placeholders -- see epl_backtest.py and the epl block
in config.yaml for the measurement and its caveats.

THE ONE THING THAT DOES NOT PORT FROM THE OTHER SPORTS: DRAWS.

MLB, NFL and CFB all score a two-outcome market. Ties are ~0.2% of NFL games
and nfl_backtest simply excludes them from its reliability pass ("no direction
to correlate a tie against"). EPL draws are 23.6% of 2,660 matches measured
here, so the same exclusion would discard a quarter of the evidence AND
condition every correlation on "given the match was decisive", which is not the
population this module scores live.

Two consequences, both deliberate:

  * The reliability pass correlates against HOME GOAL DIFFERENCE, a continuous
    outcome that uses every match and is monotone in the 3-way result, rather
    than a point-biserial against home_win. epl_backtest.py reports the
    home_win version alongside it for comparability with the NFL and CFB
    numbers; the two agree closely (see its output), which is why the swap is
    a better estimator rather than a different question.

  * A match_result pick LOSES on a draw. It is not a moneyline with a push --
    there is no push in 1X2 -- so the 60.7% hit rate in config.yaml's epl
    block is measured against that rule, not softened by it.

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


def score_game(config, sport_key, inputs):
    """Score every configured bet type for one match (v1: match_result only).
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
    min_t = cfg.get("min_threshold", 50)

    sig = _base_signals(inputs, scales)
    labels = (inputs.get("home_abbr"), inputs.get("away_abbr"))
    out = {}
    for bt, weights in bet_types.items():
        w = dict(weights)
        bt_sig = {k: sig.get(k) for k in w}
        L, n, agree = _raw_lean(bt_sig, w)
        out[bt] = _finalize(L, n, agree, min_t, labels)
    return out


# Precedence for tie-breaking the standout when two markets share the top
# score. v1 has exactly one market, so this only matters once a second EPL bet
# type is added.
_MARKET_PRECEDENCE = ("match_result",)


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
