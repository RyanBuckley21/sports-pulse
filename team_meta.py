"""Static team/nation branding lookup: official abbreviation + primary brand
color for every team we track (30 MLB clubs, current World Cup field).

Colors are each team's real, publicly documented brand color (jersey/cap/kit
primary) -- not tuned or invented for legibility. Several official colors
(navy, black) read poorly on the app's near-black background, so a single
lightness floor is applied uniformly at lookup time as a *safety check*,
not as the source of the color itself.
"""

import colorsys

# name (must match assets/logos/manifest.json keys) -> (abbreviation, official hex)
MLB_TEAMS = {
    "Arizona Diamondbacks": ("ARI", "#A71930"),
    "Athletics": ("ATH", "#003831"),
    "Atlanta Braves": ("ATL", "#CE1141"),
    "Baltimore Orioles": ("BAL", "#DF4601"),
    "Boston Red Sox": ("BOS", "#BD3039"),
    "Chicago Cubs": ("CHC", "#0E3386"),
    "Chicago White Sox": ("CWS", "#C4CED4"),
    "Cincinnati Reds": ("CIN", "#C6011F"),
    "Cleveland Guardians": ("CLE", "#E31937"),
    "Colorado Rockies": ("COL", "#333366"),
    "Detroit Tigers": ("DET", "#FA4616"),
    "Houston Astros": ("HOU", "#EB6E1F"),
    "Kansas City Royals": ("KC", "#004687"),
    "Los Angeles Angels": ("LAA", "#BA0021"),
    "Los Angeles Dodgers": ("LAD", "#005A9C"),
    "Miami Marlins": ("MIA", "#00A3E0"),
    "Milwaukee Brewers": ("MIL", "#FFC52F"),
    "Minnesota Twins": ("MIN", "#D31145"),
    "New York Mets": ("NYM", "#FF5910"),
    "New York Yankees": ("NYY", "#003087"),
    "Philadelphia Phillies": ("PHI", "#E81828"),
    "Pittsburgh Pirates": ("PIT", "#FDB827"),
    "San Diego Padres": ("SD", "#FFC425"),
    "San Francisco Giants": ("SF", "#FD5A1E"),
    "Seattle Mariners": ("SEA", "#005C5C"),
    "St. Louis Cardinals": ("STL", "#C41E3A"),
    "Tampa Bay Rays": ("TB", "#8FBCE6"),
    "Texas Rangers": ("TEX", "#C0111F"),
    "Toronto Blue Jays": ("TOR", "#134A8E"),
    "Washington Nationals": ("WSH", "#AB0003"),
}

# name (must match assets/logos/manifest.json keys) -> (FIFA code, kit primary hex)
WORLDCUP_TEAMS = {
    "Algeria": ("ALG", "#006233"),
    "Argentina": ("ARG", "#75AADB"),
    "Australia": ("AUS", "#FFCD00"),
    "Austria": ("AUT", "#ED2939"),
    "Belgium": ("BEL", "#ED2939"),
    "Bosnia-Herzegovina": ("BIH", "#002395"),
    "Brazil": ("BRA", "#FFDF00"),
    "Canada": ("CAN", "#FF0000"),
    "Cape Verde": ("CPV", "#003893"),
    "Colombia": ("COL", "#FCD116"),
    "Congo DR": ("COD", "#007FFF"),
    "Croatia": ("CRO", "#FF0000"),
    "Curaçao": ("CUW", "#002B7F"),
    "Czechia": ("CZE", "#D7141A"),
    "Ecuador": ("ECU", "#FFDD00"),
    "Egypt": ("EGY", "#CE1126"),
    "England": ("ENG", "#CF081F"),
    "France": ("FRA", "#0055A4"),
    "Germany": ("GER", "#FFCE00"),
    "Ghana": ("GHA", "#CE1126"),
    "Haiti": ("HAI", "#00209F"),
    "Iran": ("IRN", "#239F40"),
    "Iraq": ("IRQ", "#CE1126"),
    "Ivory Coast": ("CIV", "#FF8200"),
    "Japan": ("JPN", "#003DA5"),
    "Jordan": ("JOR", "#CE1126"),
    "Mexico": ("MEX", "#006847"),
    "Morocco": ("MAR", "#C1272D"),
    "Netherlands": ("NED", "#FF6C00"),
    "New Zealand": ("NZL", "#BFC1C2"),
    "Norway": ("NOR", "#EF2B2D"),
    "Panama": ("PAN", "#DA121A"),
    "Paraguay": ("PAR", "#DA121A"),
    "Portugal": ("POR", "#FF0000"),
    "Qatar": ("QAT", "#8D1B3D"),
    "Saudi Arabia": ("KSA", "#006C35"),
    "Scotland": ("SCO", "#0065BD"),
    "Senegal": ("SEN", "#00853F"),
    "South Africa": ("RSA", "#FFB81C"),
    "South Korea": ("KOR", "#CE1126"),
    "Spain": ("ESP", "#C60B1E"),
    "Sweden": ("SWE", "#FECC02"),
    "Switzerland": ("SUI", "#FF0000"),
    "Tunisia": ("TUN", "#E70013"),
    "Türkiye": ("TUR", "#E30A17"),
    "United States": ("USA", "#B22234"),
    "Uruguay": ("URU", "#4AA5DE"),
    "Uzbekistan": ("UZB", "#0099B5"),
}

MIN_LIGHTNESS = 0.55


def _ensure_legible(hex_color):
    """Raise HSL lightness to a floor of ~55% so a team's real (possibly dark
    navy/black) brand color still reads against the app's #0A0A0B background.
    Hue/saturation are preserved -- this only brightens, never changes color."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < MIN_LIGHTNESS:
        l = MIN_LIGHTNESS
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def get_team_meta(sport_key, team_name):
    """Returns {"abbr": str, "color": "#RRGGBB"} for a known team, or None if
    we have no branding entry for it (caller should omit the chip/color)."""
    table = MLB_TEAMS if sport_key == "mlb" else WORLDCUP_TEAMS
    entry = table.get(team_name)
    if not entry:
        return None
    abbr, hex_color = entry
    return {"abbr": abbr, "color": _ensure_legible(hex_color)}
