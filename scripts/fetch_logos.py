"""One-off script: cache team logos locally so generate_stats.py never needs
network access for them on regular runs.

Run manually via the "Fetch team logos" GitHub Actions workflow (not part of
the daily deploy) whenever team logos are missing or a new World Cup team
needs adding. This script needs real internet access to mlbstatic.com and
a.espncdn.com, which the sandboxed dev environment's egress policy blocks --
it's designed to run on a GitHub Actions runner instead.

Writes:
  assets/logos/mlb/{team_id}.png
  assets/logos/nfl/{abbr}.png
  assets/logos/epl/{slug}.png
  assets/logos/cfb/{slug}.png
  assets/logos/worldcup/{slug}.png
  assets/logos/manifest.json  -- {"mlb": {team_name: relative_path, ...},
                                   "nfl": {team_abbr: relative_path, ...},
                                   "epl": {team_displayName: relative_path, ...},
                                   "cfb": {school: relative_path, ...},
                                   "worldcup": {team_name: relative_path, ...}}

Note the NFL section is keyed by ABBREVIATION, not full club name, because
that is the identifier nflverse emits and therefore the one team_meta.py and
generate_stats.team_logo_path look up. See team_meta.NFL_TEAMS.
"""

import io
import json
import os
import re

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

REQUEST_TIMEOUT = 20
LOGO_SIZE = 64
# The FBS field is season-scoped (see fetch_cfb_logos). Bump this each August
# alongside a team_meta.CFB_TEAMS refresh so the crests and the table agree.
CFB_SEASON = 2025

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGOS_DIR = os.path.join(ROOT, "assets", "logos")
MANIFEST_PATH = os.path.join(LOGOS_DIR, "manifest.json")


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def pad_to_square(img, size):
    img = img.convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - img.width) // 2, (size - img.height) // 2)
    canvas.paste(img, offset, img)
    return canvas


def fetch_mlb_logos(session, browser, manifest):
    teams = session.get(
        "https://statsapi.mlb.com/api/v1/teams", params={"sportId": 1}, timeout=REQUEST_TIMEOUT
    ).json()["teams"]

    page = browser.new_page(viewport={"width": LOGO_SIZE, "height": LOGO_SIZE})
    out_dir = os.path.join(LOGOS_DIR, "mlb")
    os.makedirs(out_dir, exist_ok=True)
    manifest.setdefault("mlb", {})

    for team in teams:
        team_id = team["id"]
        name = team["name"]
        url = f"https://www.mlbstatic.com/team-logos/{team_id}.svg"
        dest_path = os.path.join(out_dir, f"{team_id}.png")
        try:
            page.goto(url)
            page.wait_for_timeout(100)
            png_bytes = page.screenshot(omit_background=True)
            img = Image.open(io.BytesIO(png_bytes))
            pad_to_square(img, LOGO_SIZE).save(dest_path, "PNG", optimize=True)
            manifest["mlb"][name] = f"logos/mlb/{team_id}.png"
            print(f"  ok: {name} -> {dest_path}")
        except Exception as e:
            print(f"  FAILED: {name} ({url}): {e}")

    page.close()


def fetch_nfl_logos(session, manifest):
    """Cache all 32 NFL club logos from ESPN's team list.

    Keyed in the manifest by the abbreviation nflverse uses, which is what
    every NFL row in the pipeline carries -- NOT ESPN's own abbreviation,
    which disagrees on a couple of clubs (ESPN says WSH and LAR where
    nflverse says WAS and LA). _NFLVERSE_ABBR translates the handful that
    differ; anything not in it passes through unchanged. Without that step
    those clubs would cache fine and then never resolve at lookup time.
    """
    _NFLVERSE_ABBR = {"WSH": "WAS", "LAR": "LA"}

    data = session.get(
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams",
        timeout=REQUEST_TIMEOUT,
    ).json()

    out_dir = os.path.join(LOGOS_DIR, "nfl")
    os.makedirs(out_dir, exist_ok=True)
    manifest.setdefault("nfl", {})

    entries = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for entry in entries:
        team = entry.get("team", {})
        espn_abbr = team.get("abbreviation")
        logos = team.get("logos") or []
        if not espn_abbr or not logos:
            print(f"  FAILED: {team.get('displayName')} (no abbreviation/logo in payload)")
            continue
        abbr = _NFLVERSE_ABBR.get(espn_abbr, espn_abbr)
        logo_url = logos[0].get("href")
        dest_path = os.path.join(out_dir, f"{abbr}.png")
        try:
            resp = session.get(logo_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            pad_to_square(img, LOGO_SIZE).save(dest_path, "PNG", optimize=True)
            manifest["nfl"][abbr] = f"logos/nfl/{abbr}.png"
            print(f"  ok: {team.get('displayName')} [{abbr}] -> {dest_path}")
        except Exception as e:
            print(f"  FAILED: {team.get('displayName')} ({logo_url}): {e}")


def fetch_epl_logos(session, manifest):
    """Cache all 20 Premier League club crests from ESPN's team list.

    Crests matter more here than for any other sport in this repo: EPL colour
    cannot identify a club (six near-identical reds, six near-identical
    blues -- see team_meta.EPL_TEAMS), so the crest is the primary visual
    identifier and colour is only an accent.

    Keyed in the manifest by ESPN's `displayName`, which is what
    fetchers/epl.py reads off each match roster and therefore what
    team_meta/team_logo_path look up. This one uses the SAME identifier the
    stat data carries, so unlike the NFL fetcher it needs no abbreviation
    translation table.

    Re-run this after each promotion/relegation cycle: three clubs leave and
    three arrive every summer, and a newly promoted club with no cached crest
    simply renders without one until then."""
    data = session.get(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams",
        timeout=REQUEST_TIMEOUT,
    ).json()

    out_dir = os.path.join(LOGOS_DIR, "epl")
    os.makedirs(out_dir, exist_ok=True)
    manifest.setdefault("epl", {})

    entries = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for entry in entries:
        team = entry.get("team", {})
        name = team.get("displayName")
        logos = team.get("logos") or []
        if not name or not logos:
            print(f"  FAILED: {name or '(unnamed)'} (no displayName/logo in payload)")
            continue
        slug = slugify(name)
        dest_path = os.path.join(out_dir, f"{slug}.png")
        logo_url = logos[0].get("href")
        try:
            resp = session.get(logo_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            pad_to_square(img, LOGO_SIZE).save(dest_path, "PNG", optimize=True)
            manifest["epl"][name] = f"logos/epl/{slug}.png"
            print(f"  ok: {name} -> {dest_path}")
        except Exception as e:
            print(f"  FAILED: {name} ({logo_url}): {e}")


def fetch_cfb_logos(session, manifest, season):
    """Cache all 136 FBS crests from CollegeFootballData.

    Crests matter more here than anywhere else in this repo. EPL colour is
    weakly identifying; FBS colour is not identifying AT ALL -- 110 of 136
    programs collide perceptually with another program after the lightness
    floor (see team_meta.CFB_TEAMS' header for the measurements). The crest
    is the only thing that tells two programs apart, so a missing one is a
    functional failure, not a cosmetic one.

    NO RESIZING, unlike every other fetcher here. CFBD publishes each crest
    pre-rendered at eight sizes in light and dark, including exactly the
    64px LOGO_SIZE this repo uses, so the 64px light variant is written
    straight to disk. That also means no Pillow dependency on this path.

    SEASON-SCOPED for the same reason team_meta.CFB_TEAMS is: /teams?year=
    returns the point-in-time FBS field, while the unscoped endpoint runs
    ahead of the data and would cache crests for programs the schedule does
    not yet contain -- while missing ones it does. Pass the same season the
    table was generated for, or the two drift apart.

    Keyed in the manifest by `school`, which is what CFBD returns and what
    cfbfastR's schedule emits as home_team/away_team -- the single identifier
    the whole CFB pipeline already uses. Unlike fetch_nfl_logos this needs no
    translation table.

    Needs CFBD_API_KEY, same as fetchers/cfb.py. Re-run each August alongside
    a table refresh; a program that joins FBS without a cached crest renders
    with a colour chip that identifies nothing."""
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        print("  SKIPPED: CFBD_API_KEY not set -- no CFB crests fetched")
        return

    data = session.get(
        "https://api.collegefootballdata.com/teams",
        params={"year": season},
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    ).json()
    fbs = [t for t in data if t.get("classification") == "fbs"]

    out_dir = os.path.join(LOGOS_DIR, "cfb")
    os.makedirs(out_dir, exist_ok=True)
    manifest.setdefault("cfb", {})

    for team in fbs:
        school = team.get("school")
        # The light 64px variant specifically -- the dark ones are for light
        # backgrounds and would render as near-invisible on this app's
        # near-black surface.
        urls = [u for u in (team.get("logos") or []) if "/logos/64/" in u]
        if not school or not urls:
            print("  FAILED: {} (no school/64px-light logo in payload)".format(school or "(unnamed)"))
            continue
        slug = slugify(school)
        dest_path = os.path.join(out_dir, "{}.png".format(slug))
        try:
            resp = session.get(urls[0], timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            manifest["cfb"][school] = "logos/cfb/{}.png".format(slug)
            print("  ok: {} -> {}".format(school, dest_path))
        except Exception as e:
            print("  FAILED: {} ({}): {}".format(school, urls[0], e))


def fetch_worldcup_logos(session, manifest):
    data = session.get(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard",
        params={"dates": "20260611-20260726"},
        timeout=REQUEST_TIMEOUT,
    ).json()

    teams_seen = {}
    for event in data.get("events", []):
        for comp in event.get("competitions", [{}])[0].get("competitors", []):
            team = comp.get("team", {})
            name = team.get("displayName")
            logo = team.get("logo")
            if name and logo:
                teams_seen[name] = logo

    out_dir = os.path.join(LOGOS_DIR, "worldcup")
    os.makedirs(out_dir, exist_ok=True)
    manifest.setdefault("worldcup", {})

    for name, logo_url in teams_seen.items():
        slug = slugify(name)
        dest_path = os.path.join(out_dir, f"{slug}.png")
        try:
            resp = session.get(logo_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            pad_to_square(img, LOGO_SIZE).save(dest_path, "PNG", optimize=True)
            manifest["worldcup"][name] = f"logos/worldcup/{slug}.png"
            print(f"  ok: {name} -> {dest_path}")
        except Exception as e:
            print(f"  FAILED: {name} ({logo_url}): {e}")


def main():
    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)

    session = requests.Session()

    print("Fetching MLB team logos...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        fetch_mlb_logos(session, browser, manifest)
        browser.close()

    print("Fetching NFL team logos...")
    fetch_nfl_logos(session, manifest)

    print("Fetching Premier League club crests...")
    fetch_epl_logos(session, manifest)

    print("Fetching FBS crests...")
    fetch_cfb_logos(session, manifest, CFB_SEASON)

    print("Fetching World Cup team logos...")
    fetch_worldcup_logos(session, manifest)

    os.makedirs(LOGOS_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print("Wrote manifest with {} MLB, {} NFL, {} EPL, {} FBS and {} World Cup logos".format(
        len(manifest.get("mlb", {})), len(manifest.get("nfl", {})),
        len(manifest.get("epl", {})), len(manifest.get("cfb", {})),
        len(manifest.get("worldcup", {}))))


if __name__ == "__main__":
    main()
