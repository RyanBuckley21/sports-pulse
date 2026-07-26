"""Measures the design-token set and reports every violation.

    python3 -m tools.tokens.audit            # full report
    python3 -m tools.tokens.audit --teams    # only the team-colour section
    python3 -m tools.tokens.audit --quiet    # violations only

Exit status is 1 if anything fails, so this can gate a commit.

Three checks:

1. Contrast — every foreground token against every surface, worst case
   reported. Catches text that is unreadable on the panel level it actually
   sits on rather than the one it was designed against.

2. Semantic separation — CIEDE2000 between every pair of meaning-carrying
   tokens. Two tokens that encode different things must not look alike. This
   is what caught --warning sitting on top of --gold.

3. Team-colour collisions — the 30 MLB brand colours are injected inline by
   app.js and bypass the token system entirely, so a club's brand colour can
   land on top of a semantic token. Kept here because Phase 3 needs exactly
   this measurement to decide where team colour is allowed to appear.
"""

import sys

from . import colorkit as ck
from . import tokens as T

try:
    import team_meta
except ImportError:  # running outside the repo root
    team_meta = None


GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _c(text, color, enabled):
    return "%s%s%s" % (color, text, RESET) if enabled else text


def check_contrast(color=True, quiet=False):
    """Every foreground against every surface. Returns a list of failures."""
    failures = []
    if not quiet:
        print("\nCONTRAST  (WCAG 2.x, floor %.1f:1)" % T.MIN_CONTRAST)
        print("  %-18s %8s %8s %10s   %s" % ("token", "bg", "surface", "surface-2", "worst"))
        print("  " + "-" * 62)
    for name, value in T.FOREGROUNDS:
        ratios = [ck.contrast_ratio(value, s) for s in T.SURFACES]
        worst = min(ratios)
        ok = worst >= T.MIN_CONTRAST
        if not ok:
            failures.append("%s: %.2f:1 worst case, below %.1f"
                            % (name, worst, T.MIN_CONTRAST))
        if not quiet:
            verdict = _c("%5.2f  %s" % (worst, ck.wcag_verdict(worst)),
                         GREEN if ok else RED, color)
            print("  %-18s %8.2f %8.2f %10.2f   %s"
                  % (name, ratios[0], ratios[1], ratios[2], verdict))
    return failures


def check_semantic_separation(color=True, quiet=False):
    """Every pair of meaning-carrying tokens, in CIEDE2000."""
    failures = []
    pairs = []
    items = list(T.SEMANTIC)
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (n1, v1), (n2, v2) = items[i], items[j]
            pairs.append((ck.delta_e(v1, v2), n1, n2, v1, v2))
    pairs.sort()

    if not quiet:
        print("\nSEMANTIC SEPARATION  (CIEDE2000, floor %.1f)" % T.MIN_SEMANTIC_DELTA_E)
        print("  %-34s %8s %10s" % ("pair", "deltaE", "hsl gap"))
        print("  " + "-" * 56)
    for de, n1, n2, v1, v2 in pairs:
        ok = de >= T.MIN_SEMANTIC_DELTA_E
        if not ok:
            failures.append("%s vs %s: dE %.2f, below %.1f"
                            % (n1, n2, de, T.MIN_SEMANTIC_DELTA_E))
        if not quiet:
            label = "%s / %s" % (n1, n2)
            print("  %-34s %8s %9d deg"
                  % (label,
                     _c("%6.2f" % de, GREEN if ok else RED, color),
                     ck.hsl_hue_delta(v1, v2)))
    return failures


def check_team_colors(color=True, quiet=False):
    """Team brand colours against the semantic tokens.

    Not a pass/fail gate — these hexes are external brand facts we do not
    control, so collisions are reported as findings for Phase 3 rather than
    treated as defects to fix in the token file.
    """
    if team_meta is None:
        print("\nTEAM COLOURS: team_meta not importable; run from the repo root.")
        return []

    warm = []
    dim = []
    for club, (abbr, hexv) in team_meta.MLB_TEAMS.items():
        hits = [(n, ck.delta_e(hexv, v)) for n, v in T.SEMANTIC
                if ck.delta_e(hexv, v) < T.MIN_SEMANTIC_DELTA_E]
        worst_surface = min(ck.contrast_ratio(hexv, s) for s in T.SURFACES)
        if hits:
            warm.append((abbr, hexv, sorted(hits, key=lambda x: x[1])))
        elif worst_surface < 2.0:
            dim.append((abbr, hexv, worst_surface))

    if not quiet:
        print("\nTEAM COLOURS  (%d clubs, informational — not a gate)"
              % len(team_meta.MLB_TEAMS))
        print("  Collides with a semantic token (dE < %.1f):" % T.MIN_SEMANTIC_DELTA_E)
        if warm:
            for abbr, hexv, hits in sorted(warm, key=lambda x: x[2][0][1]):
                detail = ", ".join("%s %.1f" % (n, d) for n, d in hits)
                print("    %-5s %-9s %s" % (abbr, hexv, _c(detail, YELLOW, color)))
        else:
            print("    none")
        print("  Too dark to read on --surface (< 2:1):")
        if dim:
            for abbr, hexv, r in sorted(dim, key=lambda x: x[2]):
                print("    %-5s %-9s %s" % (abbr, hexv, _c("%.2f:1" % r, YELLOW, color)))
        else:
            print("    none")
    return []


def changed_values(quiet=False):
    if quiet:
        return
    print("\nDELIBERATE CHANGES FROM THE INHERITED VALUES")
    for name, (old, why) in T.CHANGED_NOTES.items():
        new = dict(T.FOREGROUNDS).get(name, "?")
        print("  %s: %s -> %s" % (name, old, new))
        for line in _wrap(why, 74):
            print("      " + line)


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv
    color = sys.stdout.isatty() and "--no-color" not in argv
    teams_only = "--teams" in argv

    if teams_only:
        check_team_colors(color, quiet)
        return 0

    failures = []
    failures += check_contrast(color, quiet)
    failures += check_semantic_separation(color, quiet)
    check_team_colors(color, quiet)
    changed_values(quiet)

    print()
    if failures:
        print(_c("FAIL — %d violation(s)" % len(failures), RED, color))
        for f in failures:
            print("  " + f)
        return 1
    print(_c("PASS — contrast and semantic separation both clear.", GREEN, color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
