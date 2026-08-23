"""Phase 3 -- Insight merge + deterministic game/team builder.

Runs as the final step of the generation pipeline (called from
generate_stats.main). Merges the committed insight stores into `data` -- the
per-row `insight` objects and the card-ready data["insights"] sections -- and
owns the deterministic game and team builders that run on every pipeline pass.

NO AI PROSE IS GENERATED HERE ANY MORE. This module used to shell out to Claude
Code headless (`claude -p`) to write story/summary/takeaways; config.yaml has
had ai_insights.enabled false since 2026-07-28, so that path had not run in
months and was removed. What survives is the merge, which reads whatever text
the committed stores already hold and carries it forward untouched -- nothing
here invents, rewrites, or refreshes prose.

The three disable layers are kept intact in run() (see the comment there): the
config kill switch, SP_SKIP_INSIGHTS, and the `claude` CLI probe. They are what
guarantee this module makes no AI calls. Note the consequence: with the
generation code gone, re-enabling the flag surfaces the stores' existing text
rather than producing new text.

Persistence: a single committed store, data/insights.json, is both the
change-detection cache and the carry-forward source (output/ is gitignored and
runs are ephemeral, so the store must be committed to survive).
"""

import json
import os
import shutil

import pulse
import training_capture
from fetchers import cfb, mlb, nfl

# Stamped onto every player store entry as `template_version`. It used to mean
# "which prompt produced this text", and bumping it forced regeneration; with the
# prompts gone it is frozen at the last value that actually generated anything,
# and survives only so existing store entries keep validating. Nothing bumps it.
PROMPT_VERSION = "v2"
STORE_PATH = "data/insights.json"
# Game insights (full slate every run -- NOT capped like players). Their own
# committed store (keyed by gamePk), separate from the player store's name|team
# keys so pruning and change-detection stay clean.
# Same as PROMPT_VERSION, for the games store. Frozen at the last generating
# value; nothing bumps it.
GAME_PROMPT_VERSION = "v3"
GAMES_STORE_PATH = "data/insights.games.json"
# Committed per-gamePk boxscore cache (lean reliever lines) backing bullpen ERA
# (7d). A final game's boxscore is immutable, so it's fetched once and reused
# across runs; see fetchers/mlb.build_game_entities.
BOXSCORE_CACHE_PATH = "data/boxscores.json"
# Both GAMES_STORE_PATH and BOXSCORE_CACHE_PATH are sport-keyed on disk --
# {sport: {gamePk: entry}} -- the same convention data.json's own top-level
# `sports` dict already uses. Each per-sport builder in GAME_BUILDERS still
# takes/returns a FLAT {gamePk: entry} dict (fetchers/mlb.py is untouched and
# knows nothing about other sports); the nesting is purely an on-disk envelope
# unwrapped/rewrapped at this module's boundary. See _build_game_entities and
# run()'s games-store section.

# Registered per-sport game/team builders, mirroring generate_stats.py's
# SPORT_FETCHERS registry for the leaderboard pipeline. Each entry is
# `fn(config, game_date, boxscore_cache, team_entities=None) -> (entities,
# pruned_boxscore_cache, training_rows)`, the shape mlb.build_game_entities
# already returns. A second sport joins this pipeline (Signal Score games,
# Team Pulse, training capture) by adding its own entry here -- same
# mechanism, deliberately, as SPORT_FETCHERS.
#
# nfl is REGISTERED but not ACTIVE -- _active_game_sports only attempts a
# sport that is both registered here AND listed by the config key it reads,
# so nfl.build_game_entities never runs in production until that key says so
# (same staged-rollout precedent worldcup already sets in the stat-categories
# pipeline). It is wired and calibrated, pending the decision to go live.
#
# That key is `active_game_sports`, which is SEPARATE from the `active_sports`
# key gating generate_stats.py's leaderboards -- see _active_game_sports for
# why the two were split and how `active_game_sports` falls back when absent.
# Publishing a sport's leaderboards no longer switches on its betting markets.
GAME_BUILDERS = {"mlb": mlb.build_game_entities, "nfl": nfl.build_game_entities,
                 "cfb": cfb.build_game_entities}
# Only the top-N players by pulse score get insights (store entries and rendered
# cards). Caps merge work and keeps the committed store bounded -- stale entries
# below the cap are pruned on each run.
#
# Applied PER SPORT, not to the pooled set -- see _top_n_per_sport.
TOP_N = 20


def _entity_key(name, team_abbr):
    return "{}|{}".format((name or "").strip().lower(), (team_abbr or "").strip().lower())


def _fmt_value(player, kind):
    """Human display of a leaderboard value, mirroring the UI's formatting."""
    if kind == "threshold" and player.get("met") is not None and player.get("window") is not None:
        return "{}/{}".format(int(player["met"]), int(player["window"]))
    v = player.get("value")
    try:
        if kind == "rate":
            return "{:.1f}".format(float(v))
        return str(int(float(v)))
    except (TypeError, ValueError):
        return str(v)


def _pulse(best_rank):
    # Scoring stays here (it is rank-based and specific to players); NAMING is
    # pulse.pulse's, shared with games and teams -- see pulse.py.
    return pulse.pulse(max(30, min(100, 100 - (best_rank - 1) * 7)))


def build_entities(data, config=None):
    """Collapse the leaderboard (a player may appear in several categories) into
    one entity per player, with deterministic signals + pulse + a compact stats
    block (+ a gated vs-pitcher `angle`). Returns {key: entity}."""
    entities = {}
    for sport_key, sport in data.get("sports", {}).items():
        for cat in sport.get("categories", []):
            kind = cat.get("kind")
            short_label = cat.get("short_label") or cat.get("label")
            unit = cat.get("unit") or ""
            for p in cat.get("players", []):
                key = _entity_key(p.get("entity"), p.get("team_abbr"))
                disp = _fmt_value(p, kind)
                signal_value = (disp + " " + unit).strip()
                ent = entities.get(key)
                if ent is None:
                    ent = {
                        "key": key,
                        "sport": sport_key,
                        "entity": p.get("entity"),
                        "team": p.get("team"),
                        "team_abbr": p.get("team_abbr"),
                        "team_color": p.get("team_color"),  # for team-colored player UI
                        "position": p.get("position"),
                        "signals": [],
                        "stats": [],
                        "best_rank": p.get("rank") or 99,
                        # recent-form series + its category label, from the
                        # best-rank category (updated below) -> feeds the player
                        # card's bar chart and its "{STAT} · LAST n G" eyebrow.
                        "series": p.get("series"),
                        "series_label": short_label,
                        "last_game_date": p.get("last_game_date"),
                        "vs_next_starter": p.get("vs_next_starter"),
                    }
                    entities[key] = ent
                ent["signals"].append({"label": short_label, "value": signal_value, "tone": "pos"})
                ent["stats"].append({
                    "category": short_label,
                    "value": signal_value,
                    "rank": p.get("rank"),
                    "of": p.get("total_qualified"),
                })
                if (p.get("rank") or 99) < ent["best_rank"]:
                    ent["best_rank"] = p.get("rank")
                    ent["series"] = p.get("series")        # track the driving category's form
                    ent["series_label"] = short_label      # ...and its label, in lockstep
                # newest game across appearances (ISO YYYY-MM-DD sorts lexically)
                lgd = p.get("last_game_date")
                if lgd and (ent["last_game_date"] is None or lgd > ent["last_game_date"]):
                    ent["last_game_date"] = lgd
                if ent.get("vs_next_starter") is None and p.get("vs_next_starter"):
                    ent["vs_next_starter"] = p.get("vs_next_starter")
    for ent in entities.values():
        ent["pulse"] = _pulse(ent["best_rank"])
        ent["angle"] = _player_angle(ent, config)
    return entities


def _player_angle(ent, config):
    """Deterministic parallel to betting_signals.top_market: the player's career
    line vs the pitcher they face next, returned ONLY when it clears the
    player_angle bar (enough sample AND lopsided enough to be worth a sentence).
    Returns {..vs line.., tilt: 'strong'|'weak'} or None. vs_next_starter is
    MLB-only, so non-MLB players naturally return None."""
    vs = ent.get("vs_next_starter")
    if not vs:
        return None
    cfg = ((config or {}).get("player_angle") or {}).get("mlb") or {}
    min_ab_avg = cfg.get("min_ab_avg", 5)
    hot = cfg.get("hot_avg", 0.350)
    cold = cfg.get("cold_avg", 0.150)
    min_ab_hr = cfg.get("min_ab_hr", 3)
    min_hr = cfg.get("min_hr", 2)
    ab = int(vs.get("ab") or 0)
    try:
        avg = float(vs.get("avg"))
    except (TypeError, ValueError):
        avg = None
    hr = int(vs.get("hr") or 0)
    # HR path (own lower floor) takes precedence: a 2+ HR line is a loud positive.
    if ab >= min_ab_hr and hr >= min_hr:
        return {**vs, "tilt": "strong"}
    # avg path (needs a real sample).
    if ab >= min_ab_avg and avg is not None:
        if avg >= hot:
            return {**vs, "tilt": "strong"}
        if avg <= cold:
            return {**vs, "tilt": "weak"}
    return None


def _load_store(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError):
            print("  insights: existing store unreadable, starting fresh")
    return {}


def _save_store(path, store):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)


def ai_insights_enabled(config):
    """Whether the AI-prose path (Claude calls, the banned-word scan that
    guards them, and the JS AI Note blurb) is switched on. Reads
    config.yaml's ai_insights.enabled; defaults to True (today's behavior)
    when the key/block is absent, so any config that predates this flag is
    unaffected. Shared with generate_stats.py so data.json's aiInsightsEnabled
    can never drift from what this module actually does."""
    return bool((config or {}).get("ai_insights", {}).get("enabled", True))


def _carry(prev):
    """Carry-forward view of a stored player record (or None if we have nothing)."""
    if not prev:
        return None
    return {"story": prev.get("story"), "summary": prev.get("summary"),
            "takeaways": prev.get("takeaways", []),
            "matchup_note": prev.get("matchup_note")}


def _carry_game(prev):
    """Carry-forward view of a stored game record (or None if we have nothing)."""
    if not prev:
        return None
    return {"story": prev.get("story"), "summary": prev.get("summary"),
            "betting_note": prev.get("betting_note")}


def _top_n(entities, n):
    """The top-N entities by pulse score (desc), name as a deterministic
    tiebreak so the cap doesn't flap between runs on pulse ties."""
    def score(ent):
        return (ent.get("pulse") or {}).get("score", 0)
    ordered = sorted(entities.items(), key=lambda kv: (-score(kv[1]), (kv[1].get("entity") or "")))
    return dict(ordered[:n])


def _top_n_per_sport(entities, n):
    """_top_n applied WITHIN each sport rather than across the pooled set.

    A single global cap made one sport's depth a function of another's. With
    mlb and epl both active the 20 slots came out 11 mlb / 9 epl on the
    2026-08-21 payload -- not because either board ran out of players, but
    because 12 players tied on a pulse of 100 and the cap fell where it fell.
    Neither sport was showing its own top 20, and a third sport would have
    thinned both again. Per-sport makes each sport's list complete and
    independent of what the others did that day.

    Still bounded, which is the property the cap exists for: n per ACTIVE sport
    instead of n overall. The committed player store grows in proportion (two
    sports -> up to 40 entries), which is the intended cost.

    Ordering within a sport is _top_n's, unchanged. The return is the union of
    the per-sport winners -- same {key: entity} shape the caller already took,
    so nothing downstream has to know this became per-sport."""
    by_sport = {}
    for key, ent in entities.items():
        by_sport.setdefault(ent.get("sport"), {})[key] = ent
    winners = {}
    for sport_entities in by_sport.values():
        winners.update(_top_n(sport_entities, n))
    return winners


def _active_game_sports(config):
    """Sports this run's game/team builders should attempt, in configured
    order, filtered to those actually registered in GAME_BUILDERS.

    Reads `active_game_sports`, NOT `active_sports`. Those were the same key
    until this function was split off it, which meant one list silently gated
    two unrelated pipelines: generate_stats.py's leaderboards ("Who's Hot" --
    descriptive boards of raw production) and this module's scored per-game
    picks (weights, thresholds, graded outcomes, the ledger). Adding a sport
    to `active_sports` to publish its leaderboards therefore also switched on
    its betting markets, which is not a choice anyone should make by accident.

    Resolution order, and why each step is what it is:

      1. `active_game_sports`, when the key is PRESENT -- including when it is
         an empty list, which means "no scored picks at all" and is honoured
         as the explicit kill switch it reads as. That is why this is an
         `is None` check and not an `or` chain: `[] or <fallback>` would
         quietly resolve an intentional shutoff into "every sport".
      2. `active_sports`, when `active_game_sports` is absent -- so every
         config written before this key existed behaves exactly as it did,
         with no migration and no edit required.
      3. every registered builder, if neither key is set -- the pre-existing
         fallback, preserved verbatim (including its `or` semantics, so an
         empty `active_sports` still widens to all builders exactly as it
         did before; that edge case is not this change's to redefine).

    Production config sets `active_sports: [mlb]` and no `active_game_sports`,
    so this resolves to exactly ["mlb"] -- the same list, by the same path,
    as before this split.
    """
    cfg = config or {}
    configured = cfg.get("active_game_sports")
    if configured is None:
        configured = cfg.get("active_sports") or list(GAME_BUILDERS)
    return [s for s in configured if s in GAME_BUILDERS]


def _build_game_entities(config, generated_at, team_entities=None):
    """Deterministic game builder + its stores, dispatched per sport over
    GAME_BUILDERS (mirroring generate_stats.SPORT_FETCHERS). Runs everywhere,
    CI included -- there is no longer any gated step after it.
    Returns (game_entities, games_store, game_date).

    `game_entities` is the MERGE of every attempted sport's own entities into
    one flat {key: entity} dict -- not a new shape, since data.json's games
    section has always mixed sports this way (every entity already carries its
    own "sport") and downstream consumers (_build_games_section etc.) already
    treat it as sport-mixed. `games_store` is the FULL nested {sport: {key:
    entity}} store as loaded from disk (see GAMES_STORE_PATH); the per-sport
    writability/carry-forward decision is made one layer up, in run(), exactly
    where it was already being made before this function existed in its
    generalized form.

    A single bad game does NOT reach here -- mlb.build_game_entities skips it,
    reports it, and returns the rest of the slate. What reaches here is one
    SPORT's build failing outright, and that is raised rather than absorbed:
    this used to be an `except Exception` returning empty games, which meant a
    broken builder and a genuine off-day produced byte-identical output, and
    the run exited 0 either way. Nothing downstream can tell those apart --
    run() goes on to write a {} games store over the committed pre-game
    snapshot that signal_report.py grades against, and the site deploys with
    no games at all. A red run that leaves yesterday's data.json served is the
    better failure -- true per sport today exactly as it was true overall
    before, since one sport is all that is registered.

    `team_entities`, when a list is passed, is filled with the Team entities
    built from the SAME slates and caches -- see mlb.build_game_entities. It
    stays an out-parameter rather than a fourth return value so the existing
    three-value unpacking at every other call site keeps working untouched;
    every attempted sport's teams land in the SAME shared list, passed to each
    builder in turn (each one only ever .extend()s it)."""
    if config is None:
        return {}, {}, None
    game_date = generated_at.date().isoformat()
    try:
        box_cache_all = _load_store(BOXSCORE_CACHE_PATH)
        game_entities = {}
        training_rows = []
        for sport_key in _active_game_sports(config):
            builder = GAME_BUILDERS[sport_key]
            sport_entities, pruned_cache, sport_training_rows = builder(
                config, game_date, box_cache_all.get(sport_key, {}), team_entities=team_entities)
            box_cache_all[sport_key] = pruned_cache
            game_entities.update(sport_entities)
            training_rows.extend(sport_training_rows)
        _save_store(BOXSCORE_CACHE_PATH, box_cache_all)
        games_store = _load_store(GAMES_STORE_PATH)
        print("insights(games): built {} games for {} (boxscore cache: {} final games)"
              .format(len(game_entities), game_date,
                      sum(len(v) for v in box_cache_all.values())))
        if team_entities is not None:
            print("insights(teams): built {} team profiles for {}"
                  .format(len(team_entities), game_date))
        # Phase 1 training capture: append-only, never pruned, separately
        # guarded so a capture failure can't take the games section down with
        # it. Skip-if-present makes a second run of the day a no-op.
        try:
            written, skipped = training_capture.capture_features(training_rows)
            print("training(features): captured {} of {} games for {} ({} already on file)"
                  .format(written, len(game_entities), game_date, skipped))
        except Exception as e:  # noqa: BLE001 -- capture is strictly additive
            print("training(features): capture failed ({}); games section unaffected"
                  .format(str(e)[:160]))
        return game_entities, games_store, game_date
    except Exception as e:
        # Caught only to say WHICH stage died before re-raising. Without this the
        # caller sees a bare traceback from somewhere inside a fetcher and the
        # Actions summary shows nothing at all; the games build is the one stage
        # here that makes network calls, so it is worth naming explicitly.
        detail = "{}: {}".format(type(e).__name__, str(e)[:200])
        print("insights(games): builder FAILED for {} ({})".format(game_date, detail))
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error title=Game slate build failed for {}::{}. No games were "
                  "produced, so data.json and the committed games store are NOT "
                  "being written from this run.".format(game_date, detail))
        raise


def schedule_fetcher(config):
    """A `fetch_schedule(date_str)` callable over the MLB schedule endpoint,
    hydrated with the linescore (per-inning NRFI / first-five labels) and the
    probable pitcher. Built here rather than in training_capture so that module
    stays free of HTTP concerns.

    `probablePitcher` is here for training_capture._actual_starter_id: on a
    FINAL game MLB rewrites that field to whoever really started, so the
    resolver gets ground truth without a boxscore call. Verified that
    `hydrate=linescore` alone returns no probablePitcher at all (0 of 15 games
    on 2026-07-31) while `linescore,probablePitcher` returns both on 15 of 15 --
    so this is a genuine widening, not a field that was already arriving unread.
    Comma-combined hydrates are the same pattern mlb.build_game_entities already
    uses ("probablePitcher,team,venue").

    Both callers of this function feed resolve_outcomes and nothing else
    (capture_training_data.main and _resolve_training_outcomes), so widening the
    hydrate cannot affect any other consumer.
    """
    import requests  # local import: only the outcome resolver needs a session

    base_url = config["mlb"]["base_url"]
    session = requests.Session()

    def fetch_schedule(date_str):
        return mlb._get(session, f"{base_url}/schedule",
                        params={"sportId": 1, "date": date_str,
                                "hydrate": "linescore,probablePitcher"})

    return fetch_schedule


def _resolve_training_outcomes(config, generated_at):
    """Phase 1 outcome capture: label YESTERDAY's (and any older still-pending)
    captured games from the MLB Stats API before today's features are built.

    Runs first so a game is always labelled from its own completed record, and
    never from anything today's build computes. Fully guarded -- the resolver is
    additive and must never break the pipeline."""
    if config is None:
        return
    try:
        training_capture.resolve_outcomes(schedule_fetcher(config), generated_at.date())
    except Exception as e:  # noqa: BLE001 -- capture is strictly additive
        print("training(outcomes): resolver failed ({}); labels retried next run"
              .format(str(e)[:160]))


def run(data, generated_at, config=None, store_path=STORE_PATH):
    """Enrich `data` in place with per-player AND per-game insight. Returns nothing.

    The MERGE (writing committed insight text into `data` -- the per-row `insight`
    objects and the card-ready `data["insights"]["players"|"games"]` sections) is
    now the whole job, and it always happens: it is what lets deployed builds
    surface the committed insights. There is no generation branch left to skip,
    so the three disable layers below select what gets logged rather than
    choosing a path. The deterministic game and team builders run unconditionally."""
    now_iso = generated_at.isoformat()
    # Phase 1 outcome capture runs BEFORE anything else: yesterday's labels are
    # resolved from completed games first, so today's feature capture can never
    # be influenced by (or confused with) outcome data.
    _resolve_training_outcomes(config, generated_at)
    all_entities = build_entities(data, config)
    entities = _top_n_per_sport(all_entities, TOP_N)  # top-N by pulse PER SPORT (games are NOT capped)
    store = _load_store(store_path)
    total = len(entities)

    # Games: full slate, uncapped. Built deterministically here (CI included).
    # Teams ride along on the same build -- same slate, same session, same caches
    # -- rather than opening a second fetch path for the Teams view.
    team_entities = []
    game_entities, games_store, game_date = _build_game_entities(
        config, generated_at, team_entities=team_entities)

    # Partition this run's game_entities by their own "sport" tag (every entity
    # already carries one -- see mlb.build_game_entities' return shape) so each
    # sport's slate is checked against ITS OWN store partition independently.
    # One sport's slate having already started must not freeze another sport's
    # still-pregame partition, and vice versa -- with one sport registered this
    # collapses to exactly the single check that used to run here.
    entities_by_sport = {}
    for pk, ent in game_entities.items():
        entities_by_sport.setdefault(ent.get("sport"), {})[pk] = ent

    # Decided ONCE PER SPORT, before either branch below, and only about the
    # COMMITTED games store. The build above always ran and `game_entities` is
    # always returned in full, so data.json (and the live site) still reflect
    # current, in-progress state regardless of what this says. Iterated over
    # EVERY attempted sport (not just entities_by_sport's keys), so a sport
    # with zero games today still gets its writability decision -- an off day
    # clears that sport's partition to {}, same as an off day always cleared
    # the (then-single, now per-sport) store.
    updated_games_store = dict(games_store)
    for sport_key in _active_game_sports(config):
        sport_entities = entities_by_sport.get(sport_key, {})
        sport_store = games_store.get(sport_key) or {}
        writable, why_frozen = _games_store_writable(sport_entities, sport_store, game_date)
        if why_frozen:
            print("insights(games): NOT overwriting {}'s {!r} slate -- {}. The "
                  "committed store keeps its pre-game snapshot; data.json still "
                  "has live state.".format(GAMES_STORE_PATH, sport_key, why_frozen))
        if writable:
            updated_games_store[sport_key] = _carry_forward_games_store(
                sport_entities, sport_store, now_iso)
        # else: leave updated_games_store[sport_key] exactly as committed.

    # THE THREE DISABLE LAYERS, KEPT INTACT AND IN ORDER: the config kill switch
    # is evaluated first and short-circuits SP_SKIP_INSIGHTS, which short-circuits
    # the `claude` CLI probe. That ordering is why `reason` reports the FIRST
    # layer that fired rather than every applicable one.
    #
    # They no longer choose between two behaviours. What they used to gate --
    # _subprocess_env/_preflight, the _needs_regen change detection, and the
    # _generate_all/_generate_games loops -- was removed, so every path below is
    # merge-only and `skip_generation` now selects what gets logged. Deliberately
    # left computing exactly as before: these are the layers that guarantee this
    # module makes no AI calls, and rewriting them into something shorter would
    # mean re-earning that guarantee for no functional gain.
    #
    # Note what this costs: flipping ai_insights.enabled back to true no longer
    # produces new prose, it just un-hides whatever text the committed stores
    # already hold. Restoring generation means restoring the deleted code.
    ai_disabled = not ai_insights_enabled(config)
    skip_generation = ai_disabled or bool(os.environ.get("SP_SKIP_INSIGHTS")) or shutil.which("claude") is None
    if ai_disabled:
        reason = "AI Insights disabled via config"
    elif os.environ.get("SP_SKIP_INSIGHTS"):
        reason = "SP_SKIP_INSIGHTS set"
    elif skip_generation:
        reason = "`claude` CLI not found"
    else:
        # No layer fired, and there is still nothing to generate: the prose path
        # was removed, so this is the case that used to run it. Said out loud
        # rather than logged as though a layer had stopped it, because "no AI
        # calls" is true here for a different reason than in the three above.
        reason = "no AI prose path in this build"
    with_text = sum(1 for k in entities if (store.get(k) or {}).get("summary"))
    g_with_text = sum(1 for pk, ent in game_entities.items()
                      if ((games_store.get(ent.get("sport")) or {}).get(pk) or {}).get("summary"))
    print("insights: {} -> merge-only: top {} players ({} w/ text), {} games ({} w/ text), no AI calls"
          .format(reason, total, with_text, len(game_entities), g_with_text))
    insight_map = {k: _carry(store.get(k)) for k in entities}
    game_text = {pk: _carry_game((games_store.get(ent.get("sport")) or {}).get(pk))
                for pk, ent in game_entities.items()}
    # Persist the deterministic build. Without this the committed stores would
    # never advance again.
    # The PLAYER store is written unconditionally, as before: it carries AI
    # carry-forward text and change-detection fields only, nothing grades
    # against it, and it has no pre-game snapshot semantics to protect.
    _save_store(store_path, _carry_forward_store(entities, store, now_iso))
    # The GAMES store is always saved now -- the per-sport writability decision
    # above is already baked into updated_games_store (a frozen partition is
    # copied through unchanged), so this reproduces byte-identical content on a
    # no-op write rather than needing its own gate.
    _save_store(GAMES_STORE_PATH, updated_games_store)

    _write_back(data, insight_map)
    data["insights"] = _build_players_section(entities, insight_map, generated_at)
    if game_entities:
        data["insights"]["games"] = _build_games_section(game_entities, game_text)
    # Teams: already ranked and card-ready out of the builder, and carrying no AI
    # text at all, so there is no text_map to merge and no _build_*_section pass
    # to run -- it is assigned straight across, alongside players and games.
    if team_entities:
        data["insights"]["teams"] = team_entities
    data["insights"]["ui"] = _ui_meta(config)


def _ui_meta(config):
    """Sport-level presentation config the UI needs once per sport (the static
    category strip). Per-entity display data (market labels, compare rows,
    est_total wording) is resolved onto each entity, not here. A missing/empty
    sport block contributes nothing -> the strip just doesn't render for it,
    which is exactly how an unconfigured future sport should degrade."""
    ui = (config or {}).get("insights_ui") or {}
    out = {}
    for sport_key, block in ui.items():
        cats = (block or {}).get("signal_categories")
        if cats:
            out[sport_key] = {"signal_categories": cats}
    return out


def _carry_forward_store(entities, store, now_iso):
    """The player store this module persists, pruned to the current entity
    set, with only previously-generated text carried forward -- nothing is
    invented here. Lets the deterministic build (pulse/signals) reach the
    committed store, instead of leaving it frozen at whichever date AI
    generation last actually executed (2026-07-28)."""
    new_store = {}
    for key, ent in entities.items():
        prev = store.get(key)
        text = _carry(prev) or {"story": None, "summary": None, "takeaways": [], "matchup_note": None}
        new_store[key] = {
            "entity": ent.get("entity"), "team": ent.get("team"),
            "last_game_date": ent.get("last_game_date"),
            "template_version": PROMPT_VERSION,
            "generated_at": prev.get("generated_at", now_iso) if prev else now_iso,
            "story": text["story"], "summary": text["summary"], "takeaways": text["takeaways"],
            "matchup_note": text.get("matchup_note"),
        }
    return new_store


def _slate_started(game_entities):
    """Whether any game on the built slate is already past first pitch.

    Reads the `status` the builder already puts on every entity -- the raw
    schedule's abstractGameState, "Preview" / "Live" / "Final" -- so this costs
    no extra request and cannot disagree with what the build actually saw.

    A missing/unknown status counts as STARTED, deliberately. The only thing
    this gate can do is decline to overwrite a store that already covers the
    same date, so failing closed costs at most one same-day refresh (and the
    earlier snapshot is the one worth keeping anyway); failing open would let
    exactly the overwrite this exists to prevent through. A date with no store
    yet is unaffected either way -- see _games_store_writable.
    """
    return any((e.get("status") or "") != "Preview" for e in (game_entities or {}).values())


def _store_covers_date(store, game_date):
    """Whether the committed games store already holds a slate for `game_date`.

    Same rule signal_report.store_slate_dates uses to decide which date a store
    describes: entries stamp the run that produced them, and the store is pruned
    to one slate per run, so a matching `generated_at` day means this date has
    already been captured.
    """
    if not game_date:
        return False
    return any((e.get("generated_at") or "")[:10] == game_date
               for e in (store or {}).values())


def _games_store_writable(game_entities, games_store, game_date):
    """Whether this run may overwrite the committed games store.

    The games store is the historical record of what the pick WAS: signal_report
    grades against it, and it is the only place a pre-game standout survives. It
    is meant to be a pre-game snapshot, and daily-stats-and-grade.yml's header
    treats "both cron entries land before first pitch" as a hard invariant --
    but nothing enforced it, and GitHub's scheduler has been running that
    workflow hours late, so a late run was replacing a clean pre-game store with
    a mid-game one. The extra cron entries were not adding coverage there; they
    were overwriting good data with worse.

    So: once any game on the slate has started, a store that already covers this
    date is left alone. This is the same "earliest snapshot wins" rule
    training_capture.capture_features already applies, arrived at for the same
    reason -- it just has to be expressed as skip-if-covered here, because this
    store is rebuilt whole each run rather than appended to.

    A date with NO store yet always writes, even if the run is already late:
    a mid-game record of today is worth more than no record at all, and there is
    nothing better to protect.

    Returns (writable, reason) -- `reason` is None when writable.
    """
    if not _slate_started(game_entities):
        return True, None
    if not _store_covers_date(games_store, game_date):
        return True, None
    started = sorted(
        "{}@{}".format((e.get("away") or {}).get("abbr"), (e.get("home") or {}).get("abbr"))
        for e in game_entities.values() if (e.get("status") or "") != "Preview")
    return False, ("{} of {} games already underway and a store for {} is already "
                   "committed".format(len(started), len(game_entities), game_date))


def _carry_forward_games_store(entities, store, now_iso):
    """The games store this module persists, pruned to today's slate, with
    only previously-generated text carried forward. See _carry_forward_store."""
    new_store = {}
    for pk, ent in entities.items():
        prev = store.get(pk)
        text = _carry_game(prev) or {"story": None, "summary": None, "betting_note": None}
        new_store[pk] = {
            "away": ent.get("away"), "home": ent.get("home"),
            "start": ent.get("start"), "venue": ent.get("venue"),
            "probables": ent.get("probables"), "signals": ent.get("signals"),
            "pulse": ent.get("pulse"), "betting_signals": ent.get("betting_signals"),
            "standout": ent.get("standout"),
            "status": ent.get("status"),
            "template_version": GAME_PROMPT_VERSION,
            "generated_at": prev.get("generated_at", now_iso) if prev else now_iso,
            "story": text.get("story"), "summary": text.get("summary"),
            "betting_note": text.get("betting_note"),
        }
    return new_store


def _build_players_section(entities, insight_map, generated_at):
    """Card-ready player insights for the UI (data["insights"]["players"]).
    Reuses the deterministic pulse + signals from build_entities; story/summary
    come from insight_map. Sorted most-notable first. Always emitted -- in
    merge-only mode entities without committed text still appear (pulse/signals
    render; the AI block is simply omitted client-side when empty).

    ONE FLAT LIST SPANNING EVERY ACTIVE SPORT, and each row carries its own
    `sport`. That tag is not decoration: the Players view scopes the list to the
    selected league with it, the way Who's Hot scopes its boards. Rendering this
    array as-is puts a Premier League goalkeeper in a column of MLB hitters,
    which is what it did before the view learned to filter. Any new consumer
    reads `sport` or accepts the blend."""
    players = []
    for key, ent in entities.items():
        ins = insight_map.get(key) or {}
        players.append({
            "name": ent.get("entity"),
            "sport": ent.get("sport"),
            "team": ent.get("team_abbr"),
            "team_color": ent.get("team_color"),
            "pos": ent.get("position"),
            "pulse": ent.get("pulse"),
            "signals": ent.get("signals"),
            "series": ent.get("series"),
            "series_label": ent.get("series_label"),
            "vs_next_starter": ent.get("vs_next_starter"),
            "summary": ins.get("summary"),
            "story": ins.get("story"),
            "matchup_note": ins.get("matchup_note"),
        })
    players.sort(key=lambda p: (p.get("pulse") or {}).get("score", 0), reverse=True)
    return {"generated_at": generated_at.isoformat(), "players": players}


def _build_games_section(entities, text_map):
    """Card-ready game insights for the UI, ranked by Best Angle score (highest
    first, no cap). Returns a bare list assigned to data["insights"]["games"] --
    mirroring data["insights"]["players"] (also a bare list, pulse-sorted).
    Deterministic signals/pulse from the builder; story/summary from text_map
    (omitted client-side when empty).

    Ordering is a two-level fallback: Best Angle score first, then Pulse, then
    the away@home matchup string for determinism. A missing/None score at either
    level counts as 0, so games whose best_angle is None (no market cleared the
    standout bar) sort to the bottom -- but among themselves they rank by Pulse
    rather than alphabetically. Pulse is therefore still both computed and
    attached to every game (and still rendered on the card); it is just the
    secondary sort key rather than the primary one."""
    games = []
    for pk, ent in entities.items():
        t = text_map.get(pk) or {}
        games.append({
            "id": ent.get("gamePk"),
            "sport": ent.get("sport"),
            "away": ent.get("away"),
            "home": ent.get("home"),
            "start": ent.get("start"),
            "venue": ent.get("venue"),
            "probables": ent.get("probables"),
            "pulse": ent.get("pulse"),
            "signals": ent.get("signals"),
            "betting_signals": ent.get("betting_signals"),
            "standout": ent.get("standout"),
            # Resolved presentation structures (sport-neutral; built sport-aware).
            "signal_scores": ent.get("signal_scores"),
            "best_angle": ent.get("best_angle"),
            "compare": ent.get("compare"),
            "est_total": ent.get("est_total"),
            "f5_total": ent.get("f5_total"),
            "betting_note": t.get("betting_note"),
            "summary": t.get("summary"),
            "story": t.get("story"),
        })
    # Rank by Best Angle score, then Pulse, then the abbr matchup as a
    # deterministic final tiebreak so equal-scoring games don't reorder between
    # runs. best_angle is the top_market() standout and is None whenever no
    # market clears the bar; `or 0` covers both that and an explicitly null
    # score (same rule applied to pulse), so unscored games sort to the bottom
    # rather than the top -- and once there, Pulse orders them by relevance
    # instead of leaving the tail alphabetical.
    games.sort(key=lambda g: (-((g.get("best_angle") or {}).get("score") or 0),
                              -((g.get("pulse") or {}).get("score") or 0),
                              "{}@{}".format((g.get("away") or {}).get("abbr"),
                                             (g.get("home") or {}).get("abbr"))))
    return games


def _write_back(data, insight_map):
    """Attach each player's insight onto every leaderboard row for that player."""
    for sport in data.get("sports", {}).values():
        for cat in sport.get("categories", []):
            for p in cat.get("players", []):
                key = _entity_key(p.get("entity"), p.get("team_abbr"))
                p["insight"] = insight_map.get(key)
