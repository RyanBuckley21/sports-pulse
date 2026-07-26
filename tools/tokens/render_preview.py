"""Generates web/tokens.html, the reviewable design-token preview page.

    python3 -m tools.tokens.render_preview

Every number on the page — contrast ratios, CIEDE2000 separations, the
team-colour collision lists — is computed here from `tokens.py` rather than
typed into the markup, so the prose cannot drift from the swatches it
describes. Re-run after any token change.
"""

import sys

from . import colorkit as ck
from . import tokens as T

try:
    import team_meta
except ImportError:
    team_meta = None

OUTPUT = "web/tokens.html"

PROV_LABEL = {
    "app": ("app.css", "prov-app"),
    "insights": ("insights.css", "prov-ins"),
    "both": ("both, unchanged", "prov-both"),
    "changed": ("changed", "prov-changed"),
}

VALUES = {
    "--bg": T.BG, "--surface": T.SURFACE, "--surface-2": T.SURFACE_2,
    "--hairline": T.HAIRLINE, "--border": T.BORDER, "--border-chip": T.BORDER_CHIP,
    "--text": T.TEXT, "--text-secondary": T.TEXT_SECONDARY,
    "--text-tertiary": T.TEXT_TERTIARY,
    "--accent": T.ACCENT, "--heat": T.HEAT, "--gold": T.GOLD,
    "--good": T.GOOD, "--warning": T.WARNING, "--critical": T.CRITICAL,
}

NOTES = {name: note for name, _prov, note in T.PROVENANCE}
PROV = {name: prov for name, prov, _note in T.PROVENANCE}


def swatch_rows(names):
    out = []
    for name in names:
        value = VALUES[name]
        label, cls = PROV_LABEL[PROV[name]]
        out.append("""
      <div class="trow">
        <div class="tchip" style="background:%s"></div>
        <div class="tmeta">
          <code class="tname">%s</code>
          <span class="tval">%s</span>
          <span class="prov %s">%s</span>
          <p class="tnote">%s</p>
        </div>
      </div>""" % (value, name, value, cls, label, NOTES[name]))
    return "".join(out)


def contrast_rows():
    out = []
    for name, value in T.FOREGROUNDS:
        ratios = [ck.contrast_ratio(value, s) for s in T.SURFACES]
        worst = min(ratios)
        verdict = ck.wcag_verdict(worst)
        vcls = "ok" if worst >= 4.5 else ("warn" if worst >= T.MIN_CONTRAST else "bad")
        flag = ' <span class="chg">changed</span>' if name in T.CHANGED_NOTES else ""
        out.append("""
        <tr>
          <td><span class="dot" style="background:%s"></span><code>%s</code>%s</td>
          <td class="num">%.2f</td><td class="num">%.2f</td><td class="num">%.2f</td>
          <td><span class="verdict %s">%s</span></td>
        </tr>""" % (value, name, flag, ratios[0], ratios[1], ratios[2], vcls, verdict))
    return "".join(out)


def separation_rows():
    items = list(T.SEMANTIC)
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (n1, v1), (n2, v2) = items[i], items[j]
            pairs.append((ck.delta_e(v1, v2), n1, n2, v1, v2))
    pairs.sort()
    out = []
    for de, n1, n2, v1, v2 in pairs:
        vcls = "ok" if de >= T.MIN_SEMANTIC_DELTA_E else "bad"
        out.append("""
        <tr>
          <td><span class="dot" style="background:%s"></span><span class="dot" style="background:%s"></span>
              <code>%s</code> <span class="vs">vs</span> <code>%s</code></td>
          <td class="num">%.2f</td>
          <td class="num dim">%d&deg;</td>
          <td><span class="verdict %s">%s</span></td>
        </tr>""" % (v1, v2, n1, n2, de, ck.hsl_hue_delta(v1, v2), vcls,
                    "clear" if vcls == "ok" else "too close"))
    return "".join(out)


def team_data():
    """(collisions, too_dark) using the same predicates as the audit CLI."""
    if team_meta is None:
        return [], []
    warm, dim = [], []
    for club, (abbr, hexv) in team_meta.MLB_TEAMS.items():
        hits = [(n, ck.delta_e(hexv, v)) for n, v in T.SEMANTIC
                if ck.delta_e(hexv, v) < T.MIN_SEMANTIC_DELTA_E]
        worst = min(ck.contrast_ratio(hexv, s) for s in T.SURFACES)
        if hits:
            warm.append((abbr, hexv, sorted(hits, key=lambda x: x[1]), club))
        elif worst < 2.0:
            dim.append((abbr, hexv, worst, club))
    return warm, dim


def team_swatches(warm, dim):
    if team_meta is None:
        return "<p class='lede'>team_meta unavailable.</p>"
    warm_map = {a: h for a, _v, h, _c in warm}
    dim_map = {a: r for a, _v, r, _c in dim}
    rows = [(club, abbr, hexv) for club, (abbr, hexv) in team_meta.MLB_TEAMS.items()]
    out = []
    for club, abbr, hexv in sorted(rows, key=lambda x: ck.hsl(x[2])[0]):
        h, s, l = ck.hsl(hexv)
        cls, badge = "", ""
        if abbr in warm_map:
            cls = " clash"
            near = ", ".join("%s %.0f" % (n.replace("--", ""), d)
                             for n, d in warm_map[abbr][:2])
            badge = '<span class="tflag">%s</span>' % near
        elif abbr in dim_map:
            cls = " toodark"
            badge = '<span class="tflag dim">%.2f:1</span>' % dim_map[abbr]
        out.append("""
        <div class="team%s">
          <div class="tbar" style="background:%s"></div>
          <div class="tabbr" style="color:%s">%s</div>
          <div class="thex">%s</div>
          <div class="thsl">H%d L%d%%</div>
          %s
        </div>""" % (cls, hexv, hexv, abbr, hexv, h, l, badge))
    return "".join(out)


def changed_rows():
    out = []
    for name, (old, why) in T.CHANGED_NOTES.items():
        new = VALUES[name]
        out.append("""
      <div class="chgrow">
        <div class="chgswatches">
          <div><div class="chgchip" style="background:%s"></div><span class="chghex">%s</span><span class="chgtag">was</span></div>
          <div class="arrow">&rarr;</div>
          <div><div class="chgchip" style="background:%s"></div><span class="chghex">%s</span><span class="chgtag now">now</span></div>
        </div>
        <div class="chgbody"><code class="tname">%s</code><p class="tnote">%s</p></div>
      </div>""" % (old, old, new, new, name, why))
    return "".join(out)


def build():
    warm, dim = team_data()
    n_teams = len(team_meta.MLB_TEAMS) if team_meta else 0
    warm_txt = ", ".join("%s (%s %.1f)" % (a, h[0][0].replace("--", ""), h[0][1])
                         for a, _v, h, _c in sorted(warm, key=lambda x: x[2][0][1]))
    dim_txt = ", ".join("%s %.2f:1" % (a, r)
                        for a, _v, r, _c in sorted(dim, key=lambda x: x[2]))

    de_ag = ck.delta_e(T.ACCENT, T.GOLD)
    de_gw_old = ck.delta_e(T.GOLD, T.CHANGED_NOTES["--warning"][0])
    de_gw_new = ck.delta_e(T.GOLD, T.WARNING)
    de_aw_new = ck.delta_e(T.ACCENT, T.WARNING)
    de_good_w = ck.delta_e(T.GOOD, T.WARNING)
    tt_old = T.CHANGED_NOTES["--text-tertiary"][0]
    tt_old_worst = min(ck.contrast_ratio(tt_old, s) for s in T.SURFACES)
    tt_new_worst = min(ck.contrast_ratio(T.TEXT_TERTIARY, s) for s in T.SURFACES)

    type_rows = "".join(
        '<div class="tyrow"><span class="tysize">%dpx</span>'
        '<span class="tysample" style="font-size:%dpx">Signal Score 74</span></div>' % (s, s)
        for s in T.TEXT_SCALE)
    disp_rows = "".join(
        '<div class="tyrow"><span class="tysize">%dpx</span>'
        '<span class="tysample disp" style="font-size:%dpx">74</span></div>' % (s, s)
        for s in T.DISPLAY_SCALE)
    numeric_rows = "".join(
        '<tr><td><code>%s</code></td><td>%s</td></tr>' % (sel, why)
        for sel, why in T.NUMERIC_SELECTORS)
    # Live panels rather than flat swatches: a glass fill is meaningless as a
    # colour chip, because what it looks like depends entirely on what is
    # behind it. Each panel sits over the same busy strip so the four levels
    # can be compared as material, which is the only way to review them.
    glass_levels = (
        ("--glass-fill-soft", T.GLASS_FILL_SOFT, T.GLASS_BLUR_SOFT,
         T.GLASS_BORDER, "collapsed game rows"),
        ("--glass-fill", T.GLASS_FILL, T.GLASS_BLUR,
         T.GLASS_BORDER, "the leaderboard panel"),
        ("--glass-fill-strong", T.GLASS_FILL_STRONG, T.GLASS_BLUR_STRONG,
         T.GLASS_BORDER_LIFT, "player cards, expanded game rows"),
        ("--glass-chrome", T.GLASS_CHROME, T.GLASS_BLUR_CHROME,
         T.GLASS_BORDER_CHROME, "the floating tab bar"),
    )
    glass_panels = "".join(
        '<div class="gpanel" style="background:%s;backdrop-filter:blur(%s);'
        '-webkit-backdrop-filter:blur(%s);border:1px solid %s">'
        '<code>%s</code><span>blur %s</span><span class="gwhere">%s</span></div>'
        % (fill, blur, blur, border, name, blur, where)
        for name, fill, blur, border, where in glass_levels)
    glass_rows = "".join(
        '<tr><td><code>%s</code></td><td class="num">%s</td><td>%s</td></tr>' % (n, v, w)
        for n, v, w in (
            ("--glass-fill-soft", T.GLASS_FILL_SOFT, "collapsed game rows"),
            ("--glass-fill", T.GLASS_FILL, "the leaderboard panel"),
            ("--glass-fill-strong", T.GLASS_FILL_STRONG, "player cards, expanded game rows"),
            ("--glass-chrome", T.GLASS_CHROME, "the floating tab bar"),
            ("--glass-border", T.GLASS_BORDER, "list-level panels"),
            ("--glass-border-lift", T.GLASS_BORDER_LIFT, "the primary/expanded surface"),
            ("--glass-border-chrome", T.GLASS_BORDER_CHROME, "the tab bar, plus its inset top highlight"),
            ("--glass-blur-soft", T.GLASS_BLUR_SOFT, "collapsed rows"),
            ("--glass-blur", T.GLASS_BLUR, "the leaderboard panel"),
            ("--glass-blur-strong", T.GLASS_BLUR_STRONG, "cards"),
            ("--glass-blur-chrome", T.GLASS_BLUR_CHROME, "the tab bar &mdash; the most blurred thing on screen"),
            ("--glass-active", T.GLASS_ACTIVE, "the active tab pill (selection, not status)"),
            ("--glass-active-soft", T.GLASS_ACTIVE_SOFT, "the active sport pill"),
            ("--glass-inset", T.GLASS_INSET, "the AI-note panel inside a card"),
            ("--glass-fallback", T.GLASS_FALLBACK, "@supports fallback for the card fills"),
            ("--glass-fallback-chrome", T.GLASS_FALLBACK_CHROME, "@supports fallback for the tab bar"),
        ))
    radii_rows = "".join(
        '<div class="rad"><div class="radbox" style="border-radius:%dpx"></div>'
        '<code>%s</code><span>%dpx</span></div>' % (v, n, v)
        for n, v in T.RADII)

    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Design tokens &mdash; Phase 1 proposal</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="{BG}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* GENERATED FILE -- edit tools/tokens/tokens.py and re-run
   `python3 -m tools.tokens.render_preview`. Hand edits here will be lost.

   Deliberately SELF-CONTAINED: links neither app.css nor insights.css, so
   reviewing it cannot be confounded by the live styles and editing it cannot
   affect them. */
{ROOT}
:root {{
  /* Documentation chrome only -- NOT a product token. This page displays hex
     values and selector names, which read better monospaced; the design
     system itself ships two faces and no mono. System stack, no webfont. */
  --font-code: ui-monospace, SFMono-Regular, Menlo, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--font-body); -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 80px; }}
h1 {{ font: 800 28px var(--font-head); letter-spacing: -0.02em; margin: 0 0 6px; }}
.sub {{ color: var(--text-secondary); font-size: 14px; line-height: 1.55; margin: 0 0 10px; }}
.stamp {{ display: inline-block; font: 500 11px var(--font-code); color: var(--text-tertiary);
  border: 1px solid var(--border); border-radius: var(--r-pill, 999px); padding: 5px 11px; margin-bottom: 30px; }}
h2 {{ font: 700 17px var(--font-head); letter-spacing: -0.01em;
  margin: 40px 0 4px; padding-top: 22px; border-top: 1px solid var(--hairline); }}
h2:first-of-type {{ border-top: 0; }}
.lede {{ color: var(--text-secondary); font-size: 13px; line-height: 1.6; margin: 0 0 18px; }}
code {{ font-family: var(--font-code); }}

.trow {{ display: flex; gap: 14px; align-items: flex-start; padding: 11px 0; border-bottom: 1px solid var(--hairline); }}
.trow:last-child {{ border-bottom: 0; }}
.tchip {{ width: 46px; height: 46px; flex: none; border-radius: 10px; border: 1px solid var(--border); }}
.tmeta {{ min-width: 0; }}
.tname {{ font: 600 13px var(--font-code); color: var(--text); }}
.tval {{ font: 400 12px var(--font-code); color: var(--text-secondary); margin-left: 8px; word-break: break-all; }}
.prov {{ display: inline-block; font: 700 9px var(--font-body); letter-spacing: 0.8px;
  text-transform: uppercase; border-radius: 999px; padding: 3px 8px; margin-left: 8px; vertical-align: 2px; }}
.prov-app {{ color: var(--accent); border: 1px solid rgba(255,122,46,0.35); }}
.prov-ins {{ color: var(--gold); border: 1px solid rgba(240,168,58,0.35); }}
.prov-both {{ color: var(--good); border: 1px solid rgba(63,208,122,0.35); }}
.prov-changed {{ color: var(--warning); border: 1px solid rgba(231,218,94,0.45); }}
.tnote {{ font-size: 12.5px; line-height: 1.5; color: var(--text-secondary); margin: 5px 0 0; }}

.callout {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 16px 18px; margin: 16px 0; font-size: 13px; line-height: 1.6; color: var(--text-secondary); }}
.callout strong {{ color: var(--text); }}
.callout.stop {{ border-color: rgba(248,113,113,0.45); }}
.callout.fixed {{ border-color: rgba(63,208,122,0.4); }}

.demo {{ background: var(--surface); border: 1px solid var(--border); border-radius: 18px; padding: 18px; margin: 16px 0; }}
.demo-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }}
.demo-title {{ font: 700 15px var(--font-head); }}
.demo-sub {{ font: 500 11px var(--font-body); color: var(--text-tertiary); }}
.demo-split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.demo-cell {{ background: var(--surface-2); border-radius: 14px; padding: 14px; text-align: center; }}
.demo-label {{ font: 700 9px var(--font-body); letter-spacing: 1px; text-transform: uppercase; color: var(--text-tertiary); }}
.gauge {{ width: 54px; height: 54px; border-radius: 50%; background: var(--heat); margin: 10px auto 8px; }}
.pulse-num {{ font: 800 34px/1 var(--font-head); letter-spacing: -0.03em; color: var(--text); }}
.gold-num {{ font: 800 34px/1 var(--font-head); letter-spacing: -0.03em; color: var(--gold); margin: 18px 0 8px; }}
.ba-pill {{ display: inline-block; font: 700 9px var(--font-body); letter-spacing: 1px; text-transform: uppercase;
  color: var(--gold); border: 1px solid rgba(240,168,58,0.35); border-radius: 999px; padding: 5px 11px; }}
.staleness {{ display: flex; align-items: center; gap: 6px; }}
.live-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--warning); box-shadow: 0 0 6px var(--warning); }}
.stale-text {{ font: 500 11px var(--font-code); color: var(--warning); }}

table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; font: 700 10px var(--font-body); letter-spacing: 0.8px; text-transform: uppercase;
  color: var(--text-tertiary); padding: 0 8px 9px 0; border-bottom: 1px solid var(--border); }}
td {{ padding: 9px 8px 9px 0; border-bottom: 1px solid var(--hairline); color: var(--text-secondary); }}
td code {{ font: 500 12px var(--font-code); color: var(--text); }}
td.num {{ font: 500 12px var(--font-code); color: var(--text-secondary); }}
td.dim {{ color: var(--text-tertiary); }}
.vs {{ color: var(--text-tertiary); font-size: 11px; }}
.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: -1px; }}
.chg {{ font: 700 8px var(--font-body); letter-spacing: 0.6px; text-transform: uppercase; color: var(--warning);
  border: 1px solid rgba(231,218,94,0.45); border-radius: 999px; padding: 2px 6px; margin-left: 6px; }}
.verdict {{ font: 700 9px var(--font-body); letter-spacing: 0.7px; text-transform: uppercase; border-radius: 999px; padding: 3px 8px; }}
.verdict.ok {{ color: var(--good); border: 1px solid rgba(63,208,122,0.35); }}
.verdict.warn {{ color: var(--warning); border: 1px solid rgba(231,218,94,0.45); }}
.verdict.bad {{ color: var(--critical); border: 1px solid rgba(248,113,113,0.4); }}

.chgrow {{ display: flex; gap: 18px; align-items: flex-start; background: var(--surface);
  border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-bottom: 12px; }}
.chgswatches {{ display: flex; align-items: center; gap: 10px; flex: none; }}
.chgswatches > div {{ text-align: center; }}
.chgchip {{ width: 44px; height: 44px; border-radius: 10px; border: 1px solid var(--border); }}
.chghex {{ display: block; font: 400 10px var(--font-code); color: var(--text-secondary); margin-top: 5px; }}
.chgtag {{ display: block; font: 700 8px var(--font-body); letter-spacing: 0.7px; text-transform: uppercase;
  color: var(--text-tertiary); margin-top: 2px; }}
.chgtag.now {{ color: var(--good); }}
.arrow {{ color: var(--text-tertiary); font-size: 17px; }}

.teams {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 9px; margin-top: 14px; }}
.team {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 10px; }}
.team.clash {{ border-color: rgba(231,218,94,0.5); }}
.team.toodark {{ border-color: rgba(139,144,156,0.4); }}
.tbar {{ height: 4px; border-radius: 3px; margin-bottom: 9px; }}
.tabbr {{ font: 700 14px var(--font-body); }}
.thex {{ font: 400 10px var(--font-code); color: var(--text-secondary); margin-top: 2px; }}
.thsl {{ font: 400 10px var(--font-code); color: var(--text-tertiary); }}
.tflag {{ display: inline-block; margin-top: 6px; font: 700 8px var(--font-body); letter-spacing: 0.4px;
  color: var(--warning); border: 1px solid rgba(231,218,94,0.4); border-radius: 999px; padding: 2px 6px; }}
.tflag.dim {{ color: var(--text-secondary); border-color: var(--border); }}

.tyrow {{ display: flex; align-items: baseline; gap: 16px; padding: 7px 0; border-bottom: 1px solid var(--hairline); }}
.tysize {{ font: 500 11px var(--font-code); color: var(--text-tertiary); width: 52px; flex: none; }}
.tysample {{ font-family: var(--font-body); font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }}
.tysample.disp {{ font-family: var(--font-head); font-weight: 800; letter-spacing: -0.03em; }}
.radii {{ display: flex; flex-wrap: wrap; gap: 18px; margin-top: 14px; }}
.rad {{ text-align: center; }}
.radbox {{ width: 62px; height: 62px; background: var(--surface-2); border: 1px solid var(--border); margin-bottom: 7px; }}
/* Glass has to be shown OVER something or the swatch is a lie: the whole
   point of the material is what the blur does to the content behind it. The
   strip below is deliberately busy and high-contrast for that reason. */
.glassdemo {{
  position: relative; display: flex; flex-wrap: wrap; gap: 14px;
  padding: 22px; margin-top: 14px; border-radius: 18px; overflow: hidden;
  background:
    radial-gradient(circle at 12% 30%, #FF7A2E 0 60px, transparent 61px),
    radial-gradient(circle at 42% 75%, #3FD07A 0 44px, transparent 45px),
    radial-gradient(circle at 72% 22%, #f0a83a 0 52px, transparent 53px),
    repeating-linear-gradient(115deg, #1c1f26 0 16px, #0c0d10 16px 32px);
}}
.gpanel {{
  flex: 1 1 190px; display: flex; flex-direction: column; gap: 5px;
  padding: 15px 16px; border-radius: 18px;
}}
.gpanel code {{ font-size: 12px; color: #f4f5f7; }}
.gpanel span {{ font-size: 11px; color: #8b909c; }}
.gwhere {{ font-style: italic; }}
.rad code {{ display: block; font: 500 11px var(--font-code); color: var(--text); }}
.rad span {{ font: 400 10px var(--font-code); color: var(--text-tertiary); }}

pre.tokens {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 16px; overflow-x: auto; font: 400 11.5px/1.6 var(--font-code); color: var(--text-secondary); }}
ol.decisions {{ padding-left: 20px; margin: 14px 0 0; }}
ol.decisions li {{ font-size: 13.5px; line-height: 1.65; color: var(--text-secondary); margin-bottom: 13px; }}
ol.decisions strong {{ color: var(--text); }}
ol.decisions li.done {{ color: var(--text-tertiary); }}
ol.decisions li.done strong {{ color: var(--text-secondary); }}
@media (max-width: 520px) {{ .demo-split {{ grid-template-columns: 1fr; }} .chgrow {{ flex-direction: column; }} }}
</style>
</head>
<body>
<div class="wrap">

  <h1>Design tokens &mdash; Phase 1 proposal</h1>
  <p class="sub">One consolidated token set for <code>app.css</code> + <code>insights.css</code>, on the confirmed Insights v6 base. Nothing here is wired into the live site yet &mdash; this page links neither stylesheet, so it can be reviewed without touching a component.</p>
  <div class="stamp">generated by tools/tokens/render_preview.py &middot; no production CSS modified</div>

  <h2>1. Surfaces</h2>
  <p class="lede">Insights' three-level ramp over app.css's two, lifted off pure black so <code>--surface</code> reads as a distinct layer without OLED crush.</p>
  {SURFACES_ROWS}

  <h2>2. Borders</h2>
  {BORDER_ROWS}

  <h2>3. Text</h2>
  {TEXT_ROWS}

  <h2>4. Accents &mdash; both permanent</h2>
  {ACCENT_ROWS}
  <div class="callout stop">
    <strong>This comment ships in the token file verbatim.</strong> A future session will see an orange and a gold {DE_AG:.1f} &Delta;E apart and try to &ldquo;clean up the duplication.&rdquo; They encode <em>different scores</em> &mdash; <code>--accent</code> is Pulse (the deterministic heat metric), <code>--gold</code> is Signal Score / Best Angle. They appear on the same card, so collapsing them destroys the reader's ability to tell which number they are looking at. Not a redundancy. Do not merge.
  </div>
  <div class="demo">
    <div class="demo-head">
      <div><div class="demo-title">Yankees @ Red Sox</div><div class="demo-sub">7:10 PM ET</div></div>
      <span class="ba-pill">Best Angle</span>
    </div>
    <div class="demo-split">
      <div class="demo-cell"><div class="demo-label">Pulse</div><div class="gauge"></div><div class="pulse-num">82</div></div>
      <div class="demo-cell"><div class="demo-label">Signal Score</div><div class="gold-num">74</div><div class="demo-sub">Team Total Over</div></div>
    </div>
  </div>

  <h2>5. Changed from the inherited values</h2>
  <p class="lede">Two tokens were deliberately altered during consolidation. Both are recorded in <code>tools/tokens/tokens.py</code> so the reason survives without archaeology.</p>
  {CHANGED_ROWS}
  <div class="callout fixed">
    <strong><code>--warning</code> is fixed, not deferred.</strong> The old <code>#FBBF24</code> sat <strong>{DE_GW_OLD:.1f} &Delta;E</strong> from <code>--gold</code> &mdash; less than half the <code>--accent</code>/<code>--gold</code> separation of {DE_AG:.1f} that the design already treats as its tightest acceptable pair. The replacement clears <strong>{DE_GW_NEW:.1f}</strong> from <code>--gold</code> and <strong>{DE_AW_NEW:.1f}</strong> from <code>--accent</code>, so it is a real margin from both rather than a fix aimed at gold alone. It also holds <strong>{DE_GOOD_W:.1f}</strong> from <code>--good</code>, which is the constraint that kept it from drifting into lime and reading as &ldquo;healthy&rdquo;.
    <div class="staleness" style="margin-top:14px">
      <span class="live-dot"></span><span class="stale-text">Updated 3h ago</span>
      <span style="margin:0 10px;color:var(--text-tertiary)">beside</span>
      <span class="gold-num" style="font-size:19px;margin:0">74</span>
      <span class="demo-sub">Signal Score</span>
    </div>
  </div>

  <h2>6. Contrast</h2>
  <p class="lede">WCAG ratios against all three surface levels; the verdict uses the worst of the three. <code>--text-tertiary</code> moved from {TT_OLD_WORST:.2f}:1 to {TT_NEW_WORST:.2f}:1 worst-case, clearing the 3.0 floor it previously failed.</p>
  <table>
    <thead><tr><th>Token</th><th>on bg</th><th>surface</th><th>surface-2</th><th>Verdict</th></tr></thead>
    <tbody>{CONTRAST_ROWS}</tbody>
  </table>

  <h2>7. Semantic separation</h2>
  <p class="lede">Every pair of meaning-carrying tokens, in CIEDE2000. HSL hue degrees are shown for readability but decisions are made on &Delta;E &mdash; HSL compresses the orange-to-yellow region badly, which is exactly where this palette lives. Floor is {FLOOR:.1f}, anchored to the <code>--accent</code>/<code>--gold</code> pair.</p>
  <table>
    <thead><tr><th>Pair</th><th>&Delta;E 2000</th><th>hue gap</th><th>Verdict</th></tr></thead>
    <tbody>{SEPARATION_ROWS}</tbody>
  </table>
  <p class="lede" style="margin-top:14px">Reference: &Delta;E under 1 is invisible, 1&ndash;2 needs side-by-side inspection, over 10 is unambiguous.</p>

  <h2>8. There is no rainbow category palette</h2>
  <p class="lede">The scoping doc lists &ldquo;per-category rainbow accent bars (red/pink/gold/blue/purple)&rdquo; as something to tokenize. No such palette exists anywhere in <code>web/</code> or <code>config.yaml</code>.</p>
  <p class="lede">Those leaderboard bars are <strong>team brand colour</strong>: <code>team_meta.py</code> holds an <code>(abbr, hex)</code> pair per club, <code>generate_stats.py:200</code> attaches it as <code>team_color</code>, and <code>app.js</code> writes it inline per row (<code>app.js:173/179/215/313</code>). It varies by who is ranked, not by category, so there is nothing to tokenize. All {N_TEAMS}, sorted by hue:</p>
  <div class="teams">{TEAM_SWATCHES}</div>
  <div class="callout">
    <strong>Left as a Phase 3 finding, per your call.</strong> These hexes bypass the token system entirely, and <strong>{N_WARM} of {N_TEAMS}</strong> land within &Delta;E {FLOOR:.1f} of a semantic token: {WARM_TXT}. A further {N_DIM} fall under 2:1 against <code>--surface</code> and nearly vanish: {DIM_TXT}. The fix is a placement rule &mdash; team colour confined to identity slots (abbr chip, rank bar), semantic tokens never sharing a hue band with it &mdash; not a change to this file. <code>python3 -m tools.tokens.audit --teams</code> reproduces this list.
  </div>

  <h2>9. Type</h2>
  <p class="lede">Every stack now terminates in a generic family. insights.css ended its Bricolage stacks at <code>'Space Grotesk'</code> with no generic, so a failed webfont request dropped headings to the browser's default <em>serif</em> &mdash; which is what happens when this page is opened offline.</p>
  <p class="lede"><strong>Monospace is dropped.</strong> app.css used JetBrains Mono in 18 selectors. Auditing what each one actually contains: only <strong>two</strong> are numeric columns where vertical alignment is even possible &mdash; <code>.row-rank</code> and <code>.breakdown-row-value</code>. Two more hold digits (<code>.bar-label</code>, <code>.bar-sublabel</code>) but sit in a horizontal flex row, each centred in its own column, so nothing lines up vertically by construction. The other <strong>14 are text</strong>: uppercase section labels, category chips, team abbreviations, breadcrumbs, the literal string &ldquo;vs leader&rdquo;. Mono was supplying texture there, not alignment.</p>
  <p class="lede">The alignment case is covered by tabular figures. Measured advances for Space Grotesk at 200px, per 1000em:</p>
  <table>
    <thead><tr><th>Treatment</th><th>digit advance range</th><th>spread</th><th>Aligns?</th></tr></thead>
    <tbody>
      <tr><td><code>Space Grotesk</code> default</td><td class="num">430 &ndash; 645</td><td class="num">215</td><td><span class="verdict bad">ragged</span></td></tr>
      <tr><td><code>Space Grotesk</code> + <code>tabular-nums</code></td><td class="num">615 &ndash; 620</td><td class="num">5</td><td><span class="verdict ok">aligns</span></td></tr>
      <tr><td><code>JetBrains Mono</code></td><td class="num">600 &ndash; 600</td><td class="num">0</td><td><span class="verdict ok">aligns</span></td></tr>
    </tbody>
  </table>
  <p class="lede" style="margin-top:14px">The <code>1</code> is the culprit: 215/1000em narrower than <code>0</code> in the default figures. Since <code>app.js</code> zero-pads ranks (<code>padStart(2,&quot;0&quot;)</code>), ranks 01&ndash;09 lead with the widest digit and 10+ lead with the narrowest, which is the worst case for a stacked column. <code>tnum</code> closes the gap to 5/1000em &mdash; 0.07px at 13px.</p>
  <div class="callout">
    <strong>The decisive evidence against &ldquo;mono is load-bearing&rdquo;:</strong> <code>.row-value</code>, the leaderboard's most prominent numeric column at 20px, has been <em>Space Grotesk with proportional figures</em> this whole time (<code>app.css:97</code>) &mdash; as have <code>.key-value</code> and the 68px <code>.hero-value</code>. Every big number on the site already renders without mono. It was never doing consistent alignment work.
    <br><br>
    The cost is real and worth stating: those 14 label selectors lose a distinctly technical, wide-tracked character and get tighter. That is a visible design change, not a swap, and it was reviewed on a rendered before/after rather than decided on the numbers alone. It also removes a ~31KB webfont from the critical path and settles the two-face/three-face conflict in favour of insights.css's stated rule.
  </div>
  {TYPE_ROWS}
  <p class="lede" style="margin-top:16px">Display scale &mdash; collapses today's 30/34/38px near-duplicates to 34:</p>
  {DISP_ROWS}
  <p class="lede" style="margin-top:18px">Selectors Phase 3 must give <code>font-variant-numeric: tabular-nums</code>. The last three are not mono today &mdash; they are numeric columns that have been running proportional figures all along, so this is an improvement on current behaviour rather than a like-for-like port:</p>
  <table>
    <thead><tr><th>Selector</th><th>Why</th></tr></thead>
    <tbody>{NUMERIC_ROWS}</tbody>
  </table>

  <h2>10. Radii</h2>
  <p class="lede">Current values: 3, 6, 7, 9, 10, 13, 14, 16, 18, 999. The pill is Insights-only &mdash; 8 uses there, zero in app.css &mdash; and is a large part of why the two sections read as different products.</p>
  <div class="radii">{RADII_ROWS}</div>

  <h2>11. Glass &mdash; the Phase 3 surfaces</h2>
  <p class="lede">The fills are <code>--surface-2</code> and <code>--surface</code> <em>at alpha</em>, not new greys &mdash; <code>rgb(28,31,38)</code> is <code>#1c1f26</code> and <code>rgb(22,24,29)</code> is <code>#16181d</code>. That identity is the whole trick: a glass panel reads as the same material as the opaque surface beside it. Three ladders, because prominence ranks the surfaces rather than one flat treatment being applied everywhere.</p>
  <div class="glassdemo">{GLASS_PANELS}</div>
  <table>
    <thead><tr><th>Token</th><th>Value</th><th>Where it lands</th></tr></thead>
    <tbody>{GLASS_ROWS}</tbody>
  </table>
  <div class="callout">
    <strong>Blur is not decoration here, it is what sells the material.</strong> A 0.45&ndash;0.6 alpha fill with <code>backdrop-filter</code> unsupported reads as a weak, muddy panel rather than as glass &mdash; so the stylesheets carry an <code>@supports not (backdrop-filter: blur(1px))</code> block that swaps in <code>--glass-fallback</code> / <code>--glass-fallback-chrome</code>, the same surfaces at near-full opacity. That is the honest fallback: opaque, not pretend-translucent.
  </div>

  <h2>12. The token block</h2>
  <p class="lede">What Phase 3 will land in the stylesheets, verbatim from <code>tools/tokens/tokens.py</code>.</p>
  <pre class="tokens">{ROOT_ESCAPED}</pre>

  <h2>13. Status</h2>
  <ol class="decisions">
    <li class="done"><strong>Visual language &mdash; confirmed.</strong> Insights v6 extended onto the leaderboard.</li>
    <li class="done"><strong><code>--warning</code> collision &mdash; fixed.</strong> {DE_GW_OLD:.1f} &rarr; {DE_GW_NEW:.1f} &Delta;E from <code>--gold</code>, and {DE_AW_NEW:.1f} from <code>--accent</code>.</li>
    <li class="done"><strong><code>--text-tertiary</code> contrast &mdash; fixed.</strong> {TT_OLD_WORST:.2f}:1 &rarr; {TT_NEW_WORST:.2f}:1 worst-case.</li>
    <li class="done"><strong>Font fallbacks &mdash; fixed.</strong> All three stacks terminate in a generic family.</li>
    <li class="done"><strong>Mono &mdash; dropped.</strong> Section 9. Two numeric columns move to <code>tabular-nums</code>; 14 label selectors move to <code>--font-body</code>.</li>
    <li class="done"><strong>Team-colour collisions &mdash; noted for Phase 3.</strong> No action this phase, per your call.</li>
    <li class="done"><strong>Icon registry rename &mdash; deferred.</strong> Kept out of this diff.</li>
  </ol>

</div>
</body>
</html>
"""

    root = T.css_root_block()
    html = html.format(
        BG=T.BG,
        ROOT=root + "\n" + ":root {\n  " + "\n  ".join(
            "%s: %dpx;" % (n, v) for n, v in T.RADII) + "\n}",
        ROOT_ESCAPED=root.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        SURFACES_ROWS=swatch_rows(["--bg", "--surface", "--surface-2"]),
        BORDER_ROWS=swatch_rows(["--hairline", "--border", "--border-chip"]),
        TEXT_ROWS=swatch_rows(["--text", "--text-secondary", "--text-tertiary"]),
        ACCENT_ROWS=swatch_rows(["--accent", "--heat", "--gold"]),
        CHANGED_ROWS=changed_rows(),
        CONTRAST_ROWS=contrast_rows(),
        SEPARATION_ROWS=separation_rows(),
        TEAM_SWATCHES=team_swatches(warm, dim),
        TYPE_ROWS=type_rows, DISP_ROWS=disp_rows, RADII_ROWS=radii_rows,
        GLASS_PANELS=glass_panels, GLASS_ROWS=glass_rows,
        NUMERIC_ROWS=numeric_rows,
        DE_AG=de_ag, DE_GW_OLD=de_gw_old, DE_GW_NEW=de_gw_new,
        DE_AW_NEW=de_aw_new, DE_GOOD_W=de_good_w,
        TT_OLD_WORST=tt_old_worst, TT_NEW_WORST=tt_new_worst,
        FLOOR=T.MIN_SEMANTIC_DELTA_E,
        N_TEAMS=n_teams, N_WARM=len(warm), N_DIM=len(dim),
        WARM_TXT=warm_txt, DIM_TXT=dim_txt,
    )
    return html


def main():
    html = build()
    with open(OUTPUT, "w") as fh:
        fh.write(html)
    print("wrote %s (%d bytes)" % (OUTPUT, len(html)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
