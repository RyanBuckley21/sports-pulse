"""One-off script: cache team logos locally so generate_stats.py never needs
network access for them on regular runs.

Run manually via the "Fetch team logos" GitHub Actions workflow (not part of
the daily deploy) whenever team logos are missing or a new World Cup team
needs adding. This script needs real internet access to mlbstatic.com and
a.espncdn.com, which the sandboxed dev environment's egress policy blocks --
it's designed to run on a GitHub Actions runner instead.

Writes:
  assets/logos/mlb/{team_id}.png
  assets/logos/worldcup/{slug}.png
  assets/logos/manifest.json  -- {"mlb": {team_name: relative_path, ...},
                                   "worldcup": {team_name: relative_path, ...}}
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

    print("Fetching World Cup team logos...")
    fetch_worldcup_logos(session, manifest)

    os.makedirs(LOGOS_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Wrote manifest with {len(manifest.get('mlb', {}))} MLB and {len(manifest.get('worldcup', {}))} World Cup logos")


if __name__ == "__main__":
    main()
