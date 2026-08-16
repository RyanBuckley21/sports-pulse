"""Static team/nation branding lookup: official abbreviation + primary brand
color for every team we track (30 MLB clubs, 32 NFL clubs, 23 Premier League
clubs across the 2025-26/2026-27 transition, archived World Cup field).

Colors are each team's real, publicly documented brand color (jersey/cap/kit
primary) -- not tuned or invented for legibility. Several official colors
(navy, black) read poorly on the app's near-black background, so a single
lightness floor is applied uniformly at lookup time as a *safety check*,
not as the source of the color itself.

One documented exception, applied to a handful of NFL clubs: where a club's
official PRIMARY is black or a near-black shade, the lightness floor lifts it
to an indistinct grey that identifies nothing (and would collide with every
other black-primary club). Those clubs use their most identifiable official
SECONDARY instead -- Raiders silver, Steelers gold, Saints gold, Jaguars teal,
Browns orange. This is the same call already made for the MLB table's White
Sox (silver #C4CED4, not their black primary), so it is an existing precedent
rather than a new rule. Every value is still a real, published club color.
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

# ABBREVIATION (must match assets/logos/manifest.json keys) -> (abbr, official hex).
#
# Keyed by abbreviation, NOT full club name, because that is what the data
# source actually emits: nflverse's stats_player_week / stats_team_week rows
# carry `team` as a short code ("KC", "PHI"), never "Kansas City Chiefs". The
# MLB table above is keyed by full name for exactly the same reason in reverse
# -- statsapi emits "Kansas City Royals". Key/value therefore repeat the abbr
# here; that redundancy keeps get_team_meta's shape identical across sports
# instead of special-casing the lookup per table.
#
# Two codes worth knowing, both nflverse's spelling rather than the more common
# broadcast one: LA is the Rams (not LAR) and WAS is the Commanders (not WSH,
# which in THIS repo is already the Washington Nationals in MLB_TEAMS -- the
# two never collide because lookups are per-sport).
NFL_TEAMS = {
    "ARI": ("ARI", "#97233F"),
    "ATL": ("ATL", "#A71930"),
    "BAL": ("BAL", "#241773"),
    "BUF": ("BUF", "#00338D"),
    "CAR": ("CAR", "#0085CA"),
    "CHI": ("CHI", "#0B162A"),
    "CIN": ("CIN", "#FB4F14"),
    "CLE": ("CLE", "#FF3C00"),   # orange secondary; primary is brown #311D00
    "DAL": ("DAL", "#041E42"),
    "DEN": ("DEN", "#FB4F14"),
    "DET": ("DET", "#0076B6"),
    "GB": ("GB", "#203731"),
    "HOU": ("HOU", "#03202F"),
    "IND": ("IND", "#002C5F"),
    "JAX": ("JAX", "#006778"),   # teal secondary; primary is black #101820
    "KC": ("KC", "#E31837"),
    "LA": ("LA", "#003594"),     # Rams
    "LAC": ("LAC", "#0080C6"),
    "LV": ("LV", "#A5ACAF"),     # silver secondary; primary is black #000000
    "MIA": ("MIA", "#008E97"),
    "MIN": ("MIN", "#4F2683"),
    "NE": ("NE", "#002244"),
    "NO": ("NO", "#D3BC8D"),     # gold secondary; primary is black #101820
    "NYG": ("NYG", "#0B2265"),
    "NYJ": ("NYJ", "#125740"),
    "PHI": ("PHI", "#004C54"),
    "PIT": ("PIT", "#FFB612"),   # gold secondary; primary is black #101820
    "SEA": ("SEA", "#69BE28"),   # action green; primary navy #002244 duplicates NE
    "SF": ("SF", "#AA0000"),
    "TB": ("TB", "#D50A0A"),
    "TEN": ("TEN", "#0C2340"),
    "WAS": ("WAS", "#5A1414"),
}

# ESPN displayName (must match assets/logos/manifest.json keys) -> (abbr, hex).
#
# Keyed by ESPN's `displayName` because that is what fetchers/epl.py reads off
# each match roster ("Tottenham Hotspur", "AFC Bournemouth") -- the same
# key-by-what-the-source-emits rule as MLB (full name) and NFL (abbreviation).
#
# MEMBERSHIP CHANGES EVERY SEASON. Three clubs are relegated and three promoted
# each summer, unlike the fixed 30/32 of MLB and NFL, so this table needs a
# refresh each August. A club that has left the division simply stops appearing
# in the data; a newly promoted one that is missing here degrades to no
# abbr/colour rather than erroring (see get_team_meta).
#
# This holds the UNION of the 2025-26 and 2026-27 fields (23 clubs, not 20) --
# deliberately, because the two disagree and both are live concerns right now.
# ESPN's team list already reports the 2026-27 division (Coventry, Hull and
# Ipswich up; Burnley, West Ham and Wolves down) while every completed match
# available to read is still 2025-26. A 20-club table for either season alone
# would be wrong for the other: pick 2025-26 and the promoted clubs render with
# no branding the moment the new season kicks off; pick 2026-27 and every
# historical board loses three clubs. Carrying both costs three unused entries
# and nothing else, since a club not in the division simply never appears.
#
# Prune the relegated three at the next refresh, once no window can still
# reach a match they played in.
#
# COLOUR CANNOT IDENTIFY AN EPL CLUB, and this table does not pretend
# otherwise -- crests are the primary identifier (see scripts/fetch_logos.py's
# fetch_epl_logos), colour is an accent. The division has six clubs in
# near-identical red and six in near-identical blue; after the lightness floor
# lifts them they cluster within a few degrees of hue no matter what is chosen.
# What IS fixed here is exact collisions: with each club's official primary,
# Bournemouth and Manchester United both landed on #DD463B. Four clubs
# therefore use a documented official alternate instead of their primary:
#
#   AFC Bournemouth   the darker official red, to break the exact tie with
#                     Manchester United
#   Aston Villa       their sky blue rather than claret, which would otherwise
#                     be a third indistinguishable claret alongside Burnley
#                     and West Ham
#   Fulham            their red trim; the white/black primary lifts to a grey
#                     that identifies nothing
#   Leeds United      their yellow; same reason, the primary is white
#
# Newcastle keeps black (lifting to grey) because it is then the ONLY grey, so
# it stays distinguishable. All 23 resolve to 23 distinct colours.
EPL_TEAMS = {
    "AFC Bournemouth": ("BOU", "#B50E12"),
    "Arsenal": ("ARS", "#EF0107"),
    "Aston Villa": ("AVL", "#95BFE5"),
    "Brentford": ("BRE", "#D20000"),
    "Brighton & Hove Albion": ("BHA", "#0057B8"),
    "Burnley": ("BUR", "#6C1D45"),               # relegated after 2025-26
    "Chelsea": ("CHE", "#034694"),
    "Coventry City": ("COV", "#78D0F3"),         # promoted for 2026-27
    "Crystal Palace": ("CRY", "#1B458F"),
    "Everton": ("EVE", "#003399"),
    "Fulham": ("FUL", "#CC0000"),
    "Hull City": ("HUL", "#F5A12D"),             # promoted for 2026-27
    "Ipswich Town": ("IPS", "#3A64A3"),          # promoted for 2026-27
    "Leeds United": ("LEE", "#FFCD00"),
    "Liverpool": ("LIV", "#C8102E"),
    "Manchester City": ("MCI", "#6CABDD"),
    "Manchester United": ("MUN", "#DA291C"),
    "Newcastle United": ("NEW", "#241F20"),
    "Nottingham Forest": ("NFO", "#DD0000"),
    "Sunderland": ("SUN", "#EB172B"),
    "Tottenham Hotspur": ("TOT", "#132257"),
    "West Ham United": ("WHU", "#7A263A"),        # relegated after 2025-26
    "Wolverhampton Wanderers": ("WOL", "#FDB913"),  # relegated after 2025-26
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
# How much saturation a color sheds as it's lifted, scaled by the size of the
# lift. A color that barely needs brightening keeps its saturation; one lifted
# from near-black sheds most of it. Without this, brightening a very dark,
# fully-saturated brand color to the lightness floor keeps S≈1.0 and produces a
# fluorescent neon (e.g. the Athletics' dark green #003831 -> #1AFFE2 aqua);
# with it, the same color lands on a faithful lighter tint (#5ABFB2). Mid-
# lightness colors (most reds) lift a little and stay vivid + distinct.
DESAT_STRENGTH = 0.7


def _ensure_legible(hex_color):
    """Raise a too-dark brand color to a ~55% lightness floor so it still reads
    against the app's #0A0A0B background, shedding saturation in proportion to
    how far it's lifted so the result is a faithful lighter tint rather than
    neon. Hue is always preserved; colors already at/above the floor are
    returned unchanged."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < MIN_LIGHTNESS:
        lift = (MIN_LIGHTNESS - l) / MIN_LIGHTNESS
        s *= 1 - lift * DESAT_STRENGTH
        l = MIN_LIGHTNESS
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


# Explicit per-sport dispatch. This was previously a binary expression
# (`MLB_TEAMS if sport_key == "mlb" else WORLDCUP_TEAMS`), which silently
# treated EVERY non-mlb sport as the World Cup -- so an "nfl" lookup would
# have searched the nation table, missed, and returned None, costing every
# NFL row its abbr and colour with no error anywhere to explain why. A dict
# makes each supported sport opt in by name; an unregistered sport now
# returns None because it genuinely has no table, not by falling through to
# an unrelated one. No live behaviour changes: mlb and worldcup were the
# only keys ever passed, and both resolve to the same tables as before.
_TEAM_TABLES = {"mlb": MLB_TEAMS, "nfl": NFL_TEAMS, "epl": EPL_TEAMS,
                "worldcup": WORLDCUP_TEAMS}


def get_team_meta(sport_key, team_name):
    """Returns {"abbr": str, "color": "#RRGGBB"} for a known team, or None if
    we have no branding entry for it (caller should omit the chip/color)."""
    table = _TEAM_TABLES.get(sport_key)
    if not table:
        return None
    entry = table.get(team_name)
    if not entry:
        return None
    abbr, hex_color = entry
    return {"abbr": abbr, "color": _ensure_legible(hex_color)}
