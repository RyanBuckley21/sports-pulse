"""One-off generator: emit team_meta.CFB_TEAMS from CollegeFootballData.

CFB is the one sport whose branding table is GENERATED rather than
hand-written, and the reason is scale: MLB has 30 clubs, NFL 32, the Premier
League 23 across a transition -- all reviewable by eye in a diff. FBS has
136. Hand-maintaining 136 literals through an annual refresh is not
reviewable and not reliably repeatable, so the table is produced by this
script and its OUTPUT is committed. The committed literal is still what
team_meta imports; nothing calls this at runtime.

Usage:

    export CFBD_API_KEY=...        # https://collegefootballdata.com/key
    python3 scripts/gen_cfb_teams.py --season 2025

Prints a ready-to-paste `CFB_TEAMS = {...}` block on stdout. Progress and
warnings go to stderr, so the stdout stream stays a clean paste.

SEASON-SCOPED ON PURPOSE -- pass the season the schedule actually reads, not
the bare /teams endpoint. CFBD's unscoped /teams returns the CURRENT
classification, which is forward-looking: it listed North Dakota State and
Sacramento State as FBS while the 2025 schedule still had them as FCS.
`?year=` returns the point-in-time field instead (136 programs for 2025,
exactly matching the 2025 schedule).

That distinction is not theoretical. The same forward-looking-field problem
hit the Premier League for real: ESPN serves the 2026-27 club list while the
lookback window still reads 2025-26 matches, so three relegated clubs
(Burnley, West Ham, Wolves) have no cached crest -- 93 of 697 real rows,
13.3%. EPL survives that because colour still identifies those clubs. CFB
would not: FBS colour identifies nothing (see team_meta.CFB_TEAMS' header),
so a program missing from this table loses its ONLY working identifier.
Season scoping is what prevents the same gap here.

REFRESH EACH AUGUST, re-running with the new season once the FBS field is
final -- same cadence as the Premier League table, for the same reason
(membership churns). Programs transition between FBS and FCS in both
directions every year.
"""

import argparse
import json
import os
import re
import sys

import requests

CFBD_TEAMS_URL = "https://api.collegefootballdata.com/teams"
REQUEST_TIMEOUT = 30

# CFBD emits the LITERAL STRING "#null" for a missing colour, which is
# truthy -- a plain `if row.get("color")` check passes it straight through
# into a hex parser. Validate the shape instead of the presence. Confirmed
# live: no FBS row carries it, but non-FBS rows do, so anyone widening this
# script beyond FBS would ingest it silently.
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def fetch_fbs_teams(session, season):
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        raise SystemExit(
            "CFBD_API_KEY is not set. Get a free key at "
            "https://collegefootballdata.com/key and export it before running."
        )
    resp = session.get(
        CFBD_TEAMS_URL,
        params={"year": season},
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    rows = resp.json()
    fbs = [r for r in rows if r.get("classification") == "fbs"]
    print("gen_cfb_teams: {} rows for {}, {} classified fbs"
          .format(len(rows), season, len(fbs)), file=sys.stderr)
    return fbs


def build_table(rows):
    """{school: (abbr, hex)} for every FBS row with a usable abbreviation and
    a well-formed colour. Anything rejected is reported on stderr rather than
    silently dropped -- a program missing from this table renders with no
    identifier at all, so a silent skip is the worst possible failure."""
    table, skipped = {}, []
    for r in rows:
        school = r.get("school")
        abbr = r.get("abbreviation")
        color = r.get("color")
        if not school:
            skipped.append(("(no school)", "missing school name"))
            continue
        if not abbr:
            skipped.append((school, "missing abbreviation"))
            continue
        if not HEX_RE.match(color or ""):
            skipped.append((school, "colour {!r} is not #RRGGBB".format(color)))
            continue
        table[school] = (abbr, color.upper())
    for school, why in skipped:
        print("gen_cfb_teams: SKIPPED {} -- {}".format(school, why), file=sys.stderr)
    if skipped:
        print("gen_cfb_teams: {} program(s) skipped; they will render with no "
              "abbr/colour/crest".format(len(skipped)), file=sys.stderr)
    return table


def emit(table, season):
    """Print the literal. json.dumps with ensure_ascii=False handles the
    awkward school names correctly -- apostrophes (Hawai'i), parentheses
    (Miami (OH)), ampersands (Texas A&M) and non-ASCII (San Jose State's
    accent) all appear in the real FBS field."""
    out = ["CFB_TEAMS = {"]
    for school in sorted(table):
        abbr, color = table[school]
        out.append('    {}: ({}, "{}"),'.format(
            json.dumps(school, ensure_ascii=False), json.dumps(abbr), color))
    out.append("}")
    print("\n".join(out))
    print("gen_cfb_teams: emitted {} programs for season {}"
          .format(len(table), season), file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", type=int, required=True,
                   help="season to scope the field to -- the season the schedule reads, "
                        "NOT the current/unscoped field (see module docstring)")
    args = p.parse_args(argv)
    session = requests.Session()
    emit(build_table(fetch_fbs_teams(session, args.season)), args.season)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
