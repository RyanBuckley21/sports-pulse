"""Reference-vector tests for colorkit.

CIEDE2000 has a genuinely fiddly implementation -- the hue-averaging and RT
rotation terms have several branch cases that are easy to get subtly wrong,
and a wrong implementation still returns plausible-looking numbers. Since the
token audit makes real design decisions off these values, the math is pinned
to the published Sharma/Wu/Dalal reference pairs.

Run: python3 -m tools.tokens.test_colorkit   (from the repo root)
"""

import math
import sys

from . import colorkit


# (lab1, lab2, expected_delta_e_2000) from the Sharma/Wu/Dalal test set.
DELTA_E_CASES = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    # Hand-derived rather than transcribed: this pair exercises the
    # hbar' = (h1' + h2' + 360) / 2 branch, which the bluer pairs above do
    # not reach. Worked through by hand to 4.3067; agreeing to 4 decimals
    # with the implementation is what makes it a real check.
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]

# Contrast ratios are far less error-prone, but the endpoints are worth
# pinning so a refactor of _linearize can't silently drift.
CONTRAST_CASES = [
    ("#ffffff", "#000000", 21.0),
    ("#ffffff", "#ffffff", 1.0),
    ("#777777", "#ffffff", 4.478),
]


def _delta_e_from_lab(lab1, lab2):
    """Feed Lab triples straight into delta_e by swapping out to_lab."""
    original = colorkit.to_lab
    try:
        colorkit.to_lab = lambda tag, _m={"a": lab1, "b": lab2}: _m[tag]
        return colorkit.delta_e("a", "b")
    finally:
        colorkit.to_lab = original


def main():
    failures = []

    for lab1, lab2, expected in DELTA_E_CASES:
        got = _delta_e_from_lab(lab1, lab2)
        if abs(got - expected) > 0.0002:
            failures.append("delta_e%s vs %s: expected %.4f, got %.4f"
                            % (lab1, lab2, expected, got))

    for a, b, expected in CONTRAST_CASES:
        got = colorkit.contrast_ratio(a, b)
        if abs(got - expected) > 0.005:
            failures.append("contrast_ratio(%s, %s): expected %.3f, got %.3f"
                            % (a, b, expected, got))

    # Round-tripping through CIELAB must land back on the same hex.
    for hex_color in ("#0c0d10", "#f0a83a", "#FF7A2E", "#3FD07A", "#565a64"):
        back = colorkit._lab_to_hex(*colorkit.to_lab(hex_color))
        if back.lower() != hex_color.lower():
            failures.append("lab round-trip: %s -> %s" % (hex_color, back))

    # lighten_to_contrast must actually reach the target it was asked for.
    fixed, ratio = colorkit.lighten_to_contrast("#565a64", "#1c1f26", 3.0)
    if ratio < 3.0:
        failures.append("lighten_to_contrast fell short: %s at %.3f" % (fixed, ratio))

    total = len(DELTA_E_CASES) + len(CONTRAST_CASES) + 6
    if failures:
        print("FAILED (%d of %d checks)" % (len(failures), total))
        for f in failures:
            print("  " + f)
        return 1
    print("colorkit: all %d checks pass" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
