"""Deterministic implied game-total estimate from existing inputs only (14d team
OPS, probable starter season ERA, 7d bullpen ERA). No AI, no odds, no
park/weather/lineup/umpire data -- a rough heuristic. It is deliberately NOT a
market line and must never be surfaced as one: the estimate is a point number
(runs, nearest integer) with a small propagated +/-1sigma range shown as
secondary context.

Method
------
Each team scores against the OPPONENT's staff. Blend the opponent's starter and
bullpen ERA by expected innings (starter ~5.5 IP of 9), then scale by the team's
own offense relative to league:
    opp_blend = w_sp*opp_SP_ERA + w_bp*opp_BP_ERA         (w_sp=5.5/9, w_bp=3.5/9)
    team_runs = opp_blend * (team_OPS / LEAGUE_OPS)
    mu_total  = away_runs + home_runs

The RANGE width is NOT an arbitrary buffer -- it is propagated from the sampling
noise of the inputs (independent sources, added in quadrature):
  - opp starter ERA  (season, fairly stable)      sigma_SP
  - opp bullpen ERA  (only 7 days -> very noisy)   sigma_BP   <- usually dominant
  - starter depth    (how many IP the SP covers)   +/- DELTA_IP innings
  - team 14d OPS     (small sample)                sigma_OPS
So a game with a wild/small-sample bullpen number gets a genuinely WIDER band than
one with stable inputs -- the width carries information, it isn't cosmetic.

Point   = round(mu)                    (nearest whole run -- the headline number)
Range   = [round(mu - Z*sigma), round(mu + Z*sigma)]   (Z = 1 propagated sigma)
No confidence score is ever attached -- the range is the honest statement of
uncertainty on its own. No park, weather, lineup, or umpire input exists here.
"""
import math

LEAGUE_OPS = 0.720   # league-average team OPS baseline (~tunable)
W_SP = 5.5 / 9.0     # starter covers ~5.5 of 9 innings
W_BP = 3.5 / 9.0
SIGMA_SP = 0.50      # season starter ERA: rough 1-sigma sampling noise
SIGMA_BP = 1.60      # 7-day bullpen ERA: small sample -> much noisier
SIGMA_OPS = 0.030    # 14-day team OPS: rough 1-sigma sampling noise
DELTA_IP = 1.0       # starter-depth uncertainty (+/- innings)
Z = 1.0              # range = +/- 1 propagated sigma

# Fixed qualifier that must travel with the range/tooltip so the number is never
# read as a market line. Deliberately NOT required beside the headline value.
NOTE = ("Rough model estimate from team form & pitching — not a betting line. "
        "No park, weather, or lineup data.")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round_half_up(x):
    # Predictable nearest-integer (half rounds up); runs are always positive.
    return int(math.floor(x + 0.5))


def implied_total(away_ops, home_ops, away_sp, home_sp, away_bp, home_bp):
    """Returns (mu, sigma) or None if any required input is missing (e.g. an
    unannounced/scratched starter) -- we never estimate around a hole."""
    ao, ho = _f(away_ops), _f(home_ops)
    asp, hsp = _f(away_sp), _f(home_sp)
    abp, hbp = _f(away_bp), _f(home_bp)
    if None in (ao, ho, asp, hsp, abp, hbp):
        return None

    def team(team_ops, opp_sp, opp_bp):
        k = team_ops / LEAGUE_OPS
        blend = W_SP * opp_sp + W_BP * opp_bp
        runs = blend * k
        # propagated 1-sigma on this team's expected runs
        s_sp = k * W_SP * SIGMA_SP
        s_bp = k * W_BP * SIGMA_BP
        s_depth = k * (DELTA_IP / 9.0) * abs(opp_sp - opp_bp)
        s_ops = blend * (SIGMA_OPS / LEAGUE_OPS)
        sigma = math.sqrt(s_sp**2 + s_bp**2 + s_depth**2 + s_ops**2)
        return runs, sigma

    ar, asig = team(ao, hsp, hbp)   # away scores vs HOME staff
    hr, hsig = team(ho, asp, abp)   # home scores vs AWAY staff
    mu = ar + hr
    sigma = math.sqrt(asig**2 + hsig**2)
    return mu, sigma


def estimate(away_ops, home_ops, away_sp, home_sp, away_bp, home_bp):
    """Entity-ready dict, or None when any input is missing.

    Shape is display-hierarchy aware: `point` is the headline ("Est. 7 runs");
    `low`/`high` are the +/-1sigma band meant to render smaller/secondary (or in
    a tooltip); `note` is the not-a-line qualifier that must accompany the
    range, not the headline. `point` always sits within [low, high] because
    rounding is monotonic."""
    r = implied_total(away_ops, home_ops, away_sp, home_sp, away_bp, home_bp)
    if r is None:
        return None
    mu, sigma = r
    return {
        "point": _round_half_up(mu),
        "low": _round_half_up(mu - Z * sigma),
        "high": _round_half_up(mu + Z * sigma),
        "note": NOTE,
    }


if __name__ == "__main__":
    import datetime, sys
    sys.path.insert(0, "/home/user/sports-pulse")
    import generate_stats as gs, generate_insights as gi
    cfg = gs.load_config()
    ents, _, _ = gi._build_game_entities(cfg, datetime.datetime.now(datetime.timezone.utc))
    print("%-9s %-14s  %6s %5s" % ("game", "headline", "mu", "sig"))
    for pk, e in ents.items():
        c = e["context"]; a = e["away"]["abbr"]; h = e["home"]["abbr"]
        est = estimate(c.get("away_road_ops_14d"), c.get("home_home_ops_14d"),
                       c.get("away_probable_era"), c.get("home_probable_era"),
                       c.get("away_bullpen_era_7d"), c.get("home_bullpen_era_7d"))
        r = implied_total(c.get("away_road_ops_14d"), c.get("home_home_ops_14d"),
                          c.get("away_probable_era"), c.get("home_probable_era"),
                          c.get("away_bullpen_era_7d"), c.get("home_bullpen_era_7d"))
        if est is None:
            print("%-9s %-14s  (skipped: missing input)" % (a + "@" + h, "--"))
        else:
            mu, sig = r
            print("%-9s Est. %d runs (%d-%d)  %6.2f %5.2f"
                  % (a + "@" + h, est["point"], est["low"], est["high"], mu, sig))
