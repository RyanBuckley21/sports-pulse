"""Color measurement primitives for the design-token work.

Why this exists: the Phase 1 token audit needed to answer "are these two
colors far enough apart to mean different things?" and HSL hue degrees are a
bad way to answer it. HSL compresses the orange-to-yellow region badly -- two
colors 14 degrees apart there can look nearly identical, while 14 degrees in
the blues is an obvious difference. Every separation judgment here therefore
runs through CIEDE2000 (perceptual delta-E) with HSL kept only as a readable
label.

Rough delta-E 2000 reference points, for reading the numbers below:
    < 1.0   invisible to the human eye
    1 - 2   visible only on close side-by-side inspection
    2 - 10  visible at a glance
    > 10    unambiguously different colors

No third-party dependencies on purpose -- this runs anywhere the repo's
Python does, with no additions to requirements.txt.
"""

import colorsys
import math

# D65 reference white, the illuminant sRGB is defined against.
_WHITE_D65 = (0.95047, 1.00000, 1.08883)


# --------------------------------------------------------------------------
# parsing / basic conversions
# --------------------------------------------------------------------------
def to_rgb(hex_color):
    """'#RRGGBB' (or 'RRGGBB', or shorthand '#RGB') -> (r, g, b) 0-255."""
    h = hex_color.lstrip("#").strip()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError("not a hex color: %r" % hex_color)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def to_hex(rgb):
    """(r, g, b) 0-255 -> '#rrggbb'."""
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _linearize(channel_0_255):
    c = channel_0_255 / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hsl(hex_color):
    """-> (hue_degrees, saturation_pct, lightness_pct). Labels only, never
    used for separation judgments -- see the module docstring."""
    r, g, b = [c / 255.0 for c in to_rgb(hex_color)]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return round(h * 360), round(s * 100), round(l * 100)


def hsl_hue_delta(a, b):
    """Shortest distance between two HSL hues, in degrees. Reported for
    readability; CIEDE2000 is what decisions are made on."""
    ha, hb = hsl(a)[0], hsl(b)[0]
    d = abs(ha - hb)
    return min(d, 360 - d)


# --------------------------------------------------------------------------
# WCAG contrast
# --------------------------------------------------------------------------
def relative_luminance(hex_color):
    r, g, b = to_rgb(hex_color)
    return (0.2126 * _linearize(r)
            + 0.7152 * _linearize(g)
            + 0.0722 * _linearize(b))


def contrast_ratio(a, b):
    """WCAG 2.x contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def wcag_verdict(ratio):
    """Plain-text verdict for a contrast ratio against the AA thresholds."""
    if ratio >= 4.5:
        return "AA"
    if ratio >= 3.0:
        return "AA large only"
    return "fails AA"


# --------------------------------------------------------------------------
# CIELAB + CIEDE2000
# --------------------------------------------------------------------------
def to_xyz(hex_color):
    r, g, b = [_linearize(c) for c in to_rgb(hex_color)]
    return (
        0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        0.0193339 * r + 0.1191920 * g + 0.9503041 * b,
    )


def to_lab(hex_color):
    """-> CIELAB (L*, a*, b*) under D65."""
    def f(t):
        return t ** (1.0 / 3.0) if t > 216.0 / 24389.0 else (841.0 / 108.0) * t + 4.0 / 29.0

    x, y, z = to_xyz(hex_color)
    xn, yn, zn = _WHITE_D65
    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def delta_e(a, b):
    """CIEDE2000 perceptual difference between two hex colors.

    This is the metric that decides whether two tokens are separable. See the
    module docstring for how to read the result.
    """
    l1, a1, b1 = to_lab(a)
    l2, a2, b2 = to_lab(b)

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0

    # G expands a* in the low-chroma region so near-greys compare sanely.
    c_bar7 = c_bar ** 7
    g = 0.5 * (1.0 - math.sqrt(c_bar7 / (c_bar7 + 25.0 ** 7))) if c_bar > 0 else 0.0

    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    def _hue(ap, bp):
        if ap == 0.0 and bp == 0.0:
            return 0.0
        return math.degrees(math.atan2(bp, ap)) % 360.0

    h1p, h2p = _hue(a1p, b1), _hue(a2p, b2)

    dlp = l2 - l1
    dcp = c2p - c1p

    if c1p * c2p == 0.0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180.0:
        dhp = h2p - h1p
    elif h2p - h1p > 180.0:
        dhp = h2p - h1p - 360.0
    else:
        dhp = h2p - h1p + 360.0
    dHp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2.0)

    lbp = (l1 + l2) / 2.0
    cbp = (c1p + c2p) / 2.0

    if c1p * c2p == 0.0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        hbp = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        hbp = (h1p + h2p + 360.0) / 2.0
    else:
        hbp = (h1p + h2p - 360.0) / 2.0

    t = (1.0
         - 0.17 * math.cos(math.radians(hbp - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * hbp))
         + 0.32 * math.cos(math.radians(3.0 * hbp + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * hbp - 63.0)))

    d_theta = 30.0 * math.exp(-(((hbp - 275.0) / 25.0) ** 2))
    cbp7 = cbp ** 7
    rc = 2.0 * math.sqrt(cbp7 / (cbp7 + 25.0 ** 7)) if cbp > 0 else 0.0

    sl = 1.0 + (0.015 * (lbp - 50.0) ** 2) / math.sqrt(20.0 + (lbp - 50.0) ** 2)
    sc = 1.0 + 0.045 * cbp
    sh = 1.0 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2.0 * d_theta)) * rc

    return math.sqrt(
        (dlp / sl) ** 2
        + (dcp / sc) ** 2
        + (dHp / sh) ** 2
        + rt * (dcp / sc) * (dHp / sh)
    )


# --------------------------------------------------------------------------
# helpers used by the audit
# --------------------------------------------------------------------------
def lighten_to_contrast(hex_color, background, target_ratio, max_steps=400):
    """Walk a color's CIELAB lightness up until it clears `target_ratio`
    against `background`, preserving hue and chroma as far as sRGB allows.

    Returns (hex, achieved_ratio). Used to repair --text-tertiary without
    hand-guessing a replacement hex.
    """
    l, a, b = to_lab(hex_color)
    for step in range(max_steps):
        candidate = _lab_to_hex(l + step * 0.25, a, b)
        ratio = contrast_ratio(candidate, background)
        if ratio >= target_ratio:
            return candidate, ratio
    return hex_color, contrast_ratio(hex_color, background)


def _lab_to_hex(l, a, b):
    """CIELAB -> '#rrggbb', clipping to the sRGB gamut."""
    def finv(t):
        return t ** 3 if t ** 3 > 216.0 / 24389.0 else (t - 4.0 / 29.0) * (108.0 / 841.0)

    fy = (l + 16.0) / 116.0
    fx, fz = fy + a / 500.0, fy - b / 200.0
    xn, yn, zn = _WHITE_D65
    x, y, z = finv(fx) * xn, finv(fy) * yn, finv(fz) * zn

    r = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    g = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    bl = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z

    def gamma(c):
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
        return c * 255.0

    return to_hex((gamma(r), gamma(g), gamma(bl)))
