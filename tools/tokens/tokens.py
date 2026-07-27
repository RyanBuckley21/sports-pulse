"""The proposed consolidated design-token set — single source of truth.

Phase 1 of the design-system work. `web/tokens.html` and the audit CLI both
read from here, so a value can never be right in one place and stale in the
other. Nothing in this module is wired into `web/app.css` or
`web/insights/insights.css` yet; landing it there is Phase 3.

Provenance codes on each token:
    app       carried over from web/app.css
    insights  carried over from web/insights/insights.css (the v6 restyle)
    both      already identical in the two files
    changed   deliberately altered during consolidation — see CHANGED_NOTES
"""

# --------------------------------------------------------------------------
# surfaces
# --------------------------------------------------------------------------
BG = "#0c0d10"
SURFACE = "#16181d"
SURFACE_2 = "#1c1f26"

#: Every surface a foreground token can land on. The audit checks contrast
#: against all of them and reports the worst case, because "passes on --bg"
#: is not good enough when cards sit on --surface-2.
SURFACES = (BG, SURFACE, SURFACE_2)

# --------------------------------------------------------------------------
# accents
# --------------------------------------------------------------------------
# --accent and --gold are BOTH PERMANENT. They are not a duplication left
# over from the two-stylesheet era and must not be collapsed into one token.
#
# They encode different scores:
#   --accent  Pulse Score, the deterministic heat metric (and the --heat gauge)
#   --gold    Signal Score / Best Angle
#
# These two numbers appear on the same card. Merging the tokens would look
# like a tidy-up and would destroy the reader's ability to tell which score
# they are looking at. If you are here to remove one of them: don't.
ACCENT = "#FF7A2E"
GOLD = "#f0a83a"
HEAT = "radial-gradient(circle at 35% 30%, #FFD9A0, #FF7A2E 55%, #E8480E)"

# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------
TEXT = "#f4f5f7"
TEXT_SECONDARY = "#8b909c"
TEXT_TERTIARY = "#676b75"

# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
GOOD = "#3FD07A"
WARNING = "#e7da5e"
CRITICAL = "#F87171"

# --------------------------------------------------------------------------
# borders
# --------------------------------------------------------------------------
HAIRLINE = "rgba(255, 255, 255, 0.06)"
BORDER = "#262932"
BORDER_CHIP = "#2c303a"

# --------------------------------------------------------------------------
# glass  (Phase 3, the "Liquid Glass" direction)
# --------------------------------------------------------------------------
# THE FILLS ARE THE SURFACE TOKENS AT ALPHA, NOT NEW GREYS. GLASS_FILL* is
# SURFACE_2 (#1c1f26 = rgb(28,31,38)); GLASS_CHROME is SURFACE (#16181d =
# rgb(22,24,29)). That is why a glass panel reads as the same material as the
# opaque surface it sits next to. If SURFACE or SURFACE_2 ever moves, these
# move with it -- they are not independent values to be re-picked by eye.
#
# Three ladders, because the direction uses prominence to rank surfaces rather
# than one flat treatment: a collapsed list row is lighter and less blurred
# than the expanded card it opens into, and the tab bar -- the only surface
# that floats over scrolling content -- is the most blurred thing on screen.
# Naming each rung keeps that ranking legible in the stylesheets instead of
# scattering bare alphas across three files.
GLASS_FILL_SOFT = "rgba(28, 31, 38, 0.45)"      # collapsed list rows
GLASS_FILL = "rgba(28, 31, 38, 0.55)"           # the leaderboard panel
GLASS_FILL_STRONG = "rgba(28, 31, 38, 0.6)"     # primary / expanded card
GLASS_CHROME = "rgba(22, 24, 29, 0.55)"         # the floating tab bar

GLASS_BORDER = "rgba(255, 255, 255, 0.08)"
GLASS_BORDER_LIFT = "rgba(255, 255, 255, 0.1)"
GLASS_BORDER_CHROME = "rgba(255, 255, 255, 0.14)"

GLASS_BLUR_SOFT = "10px"
GLASS_BLUR = "12px"
GLASS_BLUR_STRONG = "14px"
GLASS_BLUR_CHROME = "22px"

#: Active-state fills. Deliberately white-at-alpha rather than --accent: the
#: active tab and the active sport pill are SELECTION, not status, and tinting
#: them accent would put the Pulse Score's colour on a navigation surface.
GLASS_ACTIVE = "rgba(255, 255, 255, 0.12)"      # active tab
GLASS_ACTIVE_SOFT = "rgba(255, 255, 255, 0.1)"  # active sport pill

#: The AI-note panel, inset INSIDE an already-glass card. A second backdrop
#: filter nested in the first buys nothing (there is no page content behind it
#: to blur, only its parent), so this is a flat white wash and no blur.
GLASS_INSET = "rgba(255, 255, 255, 0.05)"

#: Opaque stand-ins for `@supports not (backdrop-filter: ...)`. Without blur,
#: a 0.45-0.6 alpha fill just looks like a weak, muddy panel -- the material
#: reads as translucent only because the blur sells it. These are the same
#: surfaces at near-full opacity, which is the honest fallback.
GLASS_FALLBACK = "rgba(28, 31, 38, 0.92)"
GLASS_FALLBACK_CHROME = "rgba(22, 24, 29, 0.94)"


#: Foreground tokens whose contrast is audited. (name, value)
FOREGROUNDS = (
    ("--text", TEXT),
    ("--text-secondary", TEXT_SECONDARY),
    ("--text-tertiary", TEXT_TERTIARY),
    ("--accent", ACCENT),
    ("--gold", GOLD),
    ("--good", GOOD),
    ("--warning", WARNING),
    ("--critical", CRITICAL),
)

#: Tokens that carry distinct *meaning* and must stay mutually
#: distinguishable. The audit measures every pair in this set; a low
#: separation here is a real defect, not a cosmetic nitpick.
SEMANTIC = (
    ("--accent", ACCENT),
    ("--gold", GOLD),
    ("--good", GOOD),
    ("--warning", WARNING),
    ("--critical", CRITICAL),
)

#: Minimum acceptable CIEDE2000 between two semantic tokens. Anchored to the
#: measured --accent/--gold separation (~17.8), which is the tightest pair the
#: design deliberately accepts, rounded down. Anything below this is closer
#: together than a pair we already consider borderline.
MIN_SEMANTIC_DELTA_E = 17.5

#: WCAG floor for the audit. 3.0 is the AA threshold for large text; tokens
#: only ever used at >=18px or on non-text affordances are held to this.
MIN_CONTRAST = 3.0

# (token, provenance, note)
PROVENANCE = (
    ("--bg", "insights", "app.css used #0A0A0B, near-black enough to clip on OLED."),
    ("--surface", "insights", "Cards, rows, the resting panel level."),
    ("--surface-2", "insights", "Lifted panels: expanded rows, pill cells, AI note. No app.css equivalent."),
    ("--hairline", "both", "Already byte-identical in both files."),
    ("--border", "insights", "app.css used rgba(255,255,255,0.08); solid hex renders predictably on all three surfaces."),
    ("--border-chip", "insights", "app.css used rgba(255,255,255,0.09). Same reasoning."),
    ("--text", "insights", "app.css used pure #ffffff, which glares on dark."),
    ("--text-secondary", "insights", "app.css used rgba white 0.5."),
    ("--text-tertiary", "changed", "Lifted from insights.css's #565a64 to clear WCAG 3:1."),
    ("--accent", "both", "Pulse Score + heat gauge. Permanent; see the module comment."),
    ("--heat", "app", "The Pulse gauge fill. Tied to --accent."),
    ("--gold", "insights", "Signal Score / Best Angle. Permanent; see the module comment."),
    ("--good", "app", "Fresh data."),
    ("--warning", "changed", "Shifted from app.css's #FBBF24, which collided with --gold."),
    ("--critical", "app", "Badly stale data."),
)

#: Every deliberate departure from the inherited values, so a later session
#: can find out *why* something looks different without archaeology.
CHANGED_NOTES = {
    "--warning": (
        "#FBBF24",
        "Inherited from app.css. Measured only 8.4 CIEDE2000 from --gold "
        "(vs 17.8 for the --accent/--gold pair we already consider tight), so "
        "a staleness indicator and a Signal Score read as the same colour "
        "meaning two unrelated things. Harmless while staleness lived only in "
        "a per-page header, but Phase 2's persistent shell puts them on screen "
        "together. Shifted to CIELAB hue ~100 to clear a real margin from "
        "--gold and --accent at once, while holding separation from --good so "
        "it does not drift into 'healthy' territory."
    ),
    "--text-tertiary": (
        "#565a64",
        "Inherited from insights.css, where it measured 2.39:1 against "
        "--surface-2 — under the WCAG 3.0 floor even for large text. Lightness "
        "raised in CIELAB (hue and chroma preserved) until it cleared 3.0:1 on "
        "the worst-case surface. Still 14.3 CIEDE2000 below --text-secondary, "
        "so the three-level text hierarchy is intact."
    ),
}

# --------------------------------------------------------------------------
# typography
# --------------------------------------------------------------------------
# Every stack terminates in a generic family. insights.css ended its Bricolage
# stacks at 'Space Grotesk' with no generic, so a failed webfont request
# dropped headings to the browser's default serif — visibly wrong against
# this palette, and easy to miss because it only shows up offline.
FONT_BODY = "'Space Grotesk', system-ui, -apple-system, sans-serif"
FONT_HEAD = "'Bricolage Grotesque', 'Space Grotesk', system-ui, sans-serif"

# There is deliberately no --font-mono. app.css used JetBrains Mono in 18
# selectors; auditing what each one actually contains showed that only two are
# numeric columns where digit alignment is even possible:
#
#     .row-rank             zero-padded ranks, stacked, 22px box
#     .breakdown-row-value  values right-aligned by justify-content
#
# Two more (.bar-label, .bar-sublabel) hold digits but sit in a horizontal
# flex row, each centred in its own column, so nothing ever lines up
# vertically. The remaining 14 are text: uppercase section labels, category
# chips, team abbreviations, breadcrumbs, "vs leader". Mono was supplying
# texture there, not alignment.
#
# The alignment case is fully covered by tabular figures. Measured advances
# for Space Grotesk at 200px, expressed per 1000em:
#
#     default        430 - 645   (the "1" is 215 units narrower than "0")
#     tabular-nums   615 - 620
#     JetBrains Mono 600 - 600
#
# So --font-mono is not carried into the consolidated set, and the two real
# numeric columns get NUMERIC_ALIGNMENT instead. This also drops a ~31KB
# webfont from the critical path and settles the two-face/three-face conflict
# in favour of insights.css's stated "no monospace, no serif" rule.
#
# The cost is real and is a design change, not a swap: those 14 label
# selectors lose their telemetry-ish character and get tighter. That was
# reviewed on a rendered comparison before this call was made.

#: Apply to numeric columns that must align vertically. Space Grotesk's
#: proportional figures vary by up to 215/1000em, which visibly ragged the
#: rank column; its tnum feature closes that to 5/1000em.
NUMERIC_ALIGNMENT = "font-variant-numeric: tabular-nums;"

#: Selectors that need NUMERIC_ALIGNMENT when Phase 3 migrates them. The last
#: three are not mono today but are numeric columns that would benefit --
#: .row-value in particular is the leaderboard's most prominent number column
#: and has been running proportional figures all along.
NUMERIC_SELECTORS = (
    (".row-rank", "was mono; zero-padded ranks stacked in a 22px box"),
    (".breakdown-row-value", "was mono; values right-aligned in a stacked list"),
    (".row-value", "already Space Grotesk; 20px stat column, right-aligned"),
    (".key-value", "already Space Grotesk; three-up stat cells"),
    # .hero-value was here, described as "already Space Grotesk; 68px hero
    # number". The hero was retired when the player detail page became a
    # multi-category list: it restated the value from the leaderboard row the
    # reader had just tapped, and being inherently single-category it had no
    # correct value to show once the page listed every board a player qualifies
    # on. Its numeric column is .pcat-value below. Removed rather than left in
    # place, because a selector list that names markup nothing emits stops being
    # a checklist and becomes folklore.
    (".pcat-value", "per-category value, stacked down the detail page's list"),
    (".pcat-rank", "per-category rank in the same list, right-aligned beside it"),
    # Added in Phase 3. The insights section was never audited for this in
    # Phase 1 because the list above came out of the mono inventory, which was
    # app.css-only -- insights.css has always been mono-free. These are the
    # same kind of column: same-position digits that a reader compares down a
    # list or watches change in place.
    (".pulse-score", "38px 0-100 gauge number, compared card to card down the list"),
    (".signal-value", "stat values in a stacked label/value list, right-aligned"),
    (".gr-pulse", "the game row's compact 0-100 score, one per row down the slate"),
    (".gr-chip-score", "0-100 market score, repeated across chips on one line"),
)

#: Text sizes, in px. Consolidated from 21 distinct values across the two
#: files. Open question for the user: whether FONT_MONO survives at all.
TEXT_SCALE = (9, 11, 12, 13, 14, 16, 18, 22)

#: Hero/display sizes. Collapses today's 30/34/38px near-duplicates to 34.
DISPLAY_SCALE = (28, 34, 68)

#: Corner radii. --r-pill is currently Insights-only (8 uses, zero in
#: app.css), which is a large part of why the two sections read as separate
#: products.
RADII = (
    ("--r-xs", 3),
    ("--r-sm", 7),
    ("--r-md", 10),
    ("--r-lg", 14),
    ("--r-xl", 18),
    ("--r-pill", 999),
)


def css_root_block():
    """The consolidated :root block, for the preview page and eventually for
    the real stylesheets."""
    return """:root {
  /* surfaces */
  --bg: %s;
  --surface: %s;
  --surface-2: %s;

  /* borders */
  --hairline: %s;
  --border: %s;
  --border-chip: %s;

  /* text */
  --text: %s;
  --text-secondary: %s;
  --text-tertiary: %s;

  /* accents -- BOTH PERMANENT, DO NOT MERGE.
     --accent is Pulse Score (the deterministic heat metric); --gold is
     Signal Score / Best Angle. Both numbers appear on the same card, so
     collapsing these two tokens would read as removing a duplicate and
     would in fact make the two scores indistinguishable. */
  --accent: %s;
  --gold: %s;
  --heat: %s;

  /* status */
  --good: %s;
  --warning: %s;
  --critical: %s;

  /* glass -- the Phase 3 "Liquid Glass" surfaces. The fills are --surface-2
     and --surface AT ALPHA, not new greys: that is what makes a glass panel
     read as the same material as the opaque surface beside it. Three ladders
     (fill / border / blur) because prominence ranks the surfaces -- a
     collapsed row is lighter and less blurred than the card it opens into,
     and the tab bar, the only surface floating over scrolling content, is
     the most blurred thing on screen. */
  --glass-fill-soft: %s;
  --glass-fill: %s;
  --glass-fill-strong: %s;
  --glass-chrome: %s;

  --glass-border: %s;
  --glass-border-lift: %s;
  --glass-border-chrome: %s;

  --glass-blur-soft: %s;
  --glass-blur: %s;
  --glass-blur-strong: %s;
  --glass-blur-chrome: %s;

  /* Selection, not status -- hence white-at-alpha rather than --accent. */
  --glass-active: %s;
  --glass-active-soft: %s;

  /* Inset panel inside an already-glass card: flat wash, no second blur. */
  --glass-inset: %s;

  /* @supports fallbacks. Alpha alone without blur reads as a muddy panel. */
  --glass-fallback: %s;
  --glass-fallback-chrome: %s;

  /* type -- two faces only, every stack ending in a generic family.
     No --font-mono: of the 18 monospace selectors in app.css only two were
     numeric columns, and tabular figures cover those. Numeric columns use
     `font-variant-numeric: tabular-nums` instead -- see NUMERIC_SELECTORS. */
  --font-body: %s;
  --font-head: %s;
}""" % (
        BG, SURFACE, SURFACE_2,
        HAIRLINE, BORDER, BORDER_CHIP,
        TEXT, TEXT_SECONDARY, TEXT_TERTIARY,
        ACCENT, GOLD, HEAT,
        GOOD, WARNING, CRITICAL,
        GLASS_FILL_SOFT, GLASS_FILL, GLASS_FILL_STRONG, GLASS_CHROME,
        GLASS_BORDER, GLASS_BORDER_LIFT, GLASS_BORDER_CHROME,
        GLASS_BLUR_SOFT, GLASS_BLUR, GLASS_BLUR_STRONG, GLASS_BLUR_CHROME,
        GLASS_ACTIVE, GLASS_ACTIVE_SOFT,
        GLASS_INSET,
        GLASS_FALLBACK, GLASS_FALLBACK_CHROME,
        FONT_BODY, FONT_HEAD,
    )
