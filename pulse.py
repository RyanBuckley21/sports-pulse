"""Pulse band vocabulary -- the single source of truth for what a 0-100 Pulse
score is CALLED.

Players, games and teams each compute a Pulse score their own way (leaderboard
rank, additive game heuristic, tanh against league average -- those stay where
they are and are not this module's business). What they must NOT each own is the
ladder that turns a score into a word: the same number has to mean the same
thing on every card, or the vocabulary is decoration rather than information.

Three separate copies of the ladder had drifted into
generate_insights._pulse, mlb._game_pulse and mlb._pulse_band. They happened to
still agree, but nothing made them; the web layer's copy had already drifted to
different cutoffs entirely, so a card could read "Warm" in text while its gauge
coloured itself from a different band. This module is the Python half of the
fix, and the labels it emits are what the web layer now keys its colours off --
so the ladder exists once, here, and everything else reads it.

THE BANDS
---------
Cutoffs are anchored to the score's own construction, not to any one day's
data. A Pulse of 50 is league average by definition (team pulse maps a
zero-centred signal with 50 + 50*combined), so each cutoff is best read as a
distance from average:

    Scorching  >= 85   (+35)
    Hot        >= 70   (+20)
    Warm       >= 55   (+5)
    Notable    >= 30   (-20)
    Cold        < 30

Only the bottom cutoff is new. Cold exists because "Notable" was carrying the
entire range below average on its own -- fine while nothing reached down there
(a game's score cannot fall below 55 at all, and players run high because the
board is capped at rank 10), but teams span the real range and 18 of 30 landed
in one bucket, from "a bit below average" at 52 down to "worst in the league"
at 13. Those are not the same statement.

30 is the mirror of Hot: +20 above average earns a distinct word, so -20 below
it should too. That is a deliberate choice of PRINCIPLE over curve-fitting --
the real 30-team distribution is smooth below 55 (its largest gaps, +6 and +5,
sit out in the 13-24 tail rather than at any class boundary), so there is no
natural break to find and a cutoff picked from one slate's gaps would be
fitting noise.
"""

# Highest floor first; band() takes the first floor a score clears, so the
# final entry must have floor 0 and acts as the catch-all.
PULSE_BANDS = (
    (85, "Scorching"),
    (70, "Hot"),
    (55, "Warm"),
    (30, "Notable"),
    (0, "Cold"),
)

# The full vocabulary, worst-to-best. Exposed so a caller (or a test, or the web
# layer's colour map) can enumerate the bands without re-deriving them.
LABELS = tuple(label for _, label in reversed(PULSE_BANDS))


def band(score):
    """The label for a 0-100 Pulse score. A None/unscored pulse has no band and
    is the caller's business, not this function's -- pass a number."""
    for floor, label in PULSE_BANDS:
        if score >= floor:
            return label
    return PULSE_BANDS[-1][1]


def pulse(score):
    """The {score, label} pair every Pulse-producing path emits. Callers compute
    the score however their entity works and hand it here to be named."""
    return {"score": score, "label": band(score)}
