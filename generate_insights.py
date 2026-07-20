"""Phase 3 -- AI Insight Generator.

Runs as the final step of the generation pipeline (called from
generate_stats.main). Turns already-computed Sports Pulse player stats into
short, plain-language INTERPRETATION (story / summary / key-takeaways) via
Claude Code headless (`claude -p`). The AI never calculates: every number it
may cite is computed here in code and passed in; the model only writes prose.

Change detection: an entity (a unique player) is only re-generated when a new
game has been played since the last run (or it's new, or the prompt template
changed). Otherwise its previous text is carried forward with no model call.

Persistence: a single committed store, data/insights.json, is both the
change-detection cache and the carry-forward source (output/ is gitignored and
runs are ephemeral, so the store must be committed to survive).

CI-safe: if the claude CLI isn't available (or SP_SKIP_INSIGHTS is set), the
step logs a skip and returns without touching anything -- the data pipeline is
unaffected.
"""

import datetime
import json
import os
import re
import shutil
import subprocess

from fetchers import mlb

PROMPT_VERSION = "v1"
STORE_PATH = "data/insights.json"
# Game insights (full slate every run -- NOT capped like players). Their own
# committed store (keyed by gamePk), separate from the player store's name|team
# keys so pruning and change-detection stay clean.
GAME_PROMPT_VERSION = "v1"
GAMES_STORE_PATH = "data/insights.games.json"
# Committed per-gamePk boxscore cache (lean reliever lines) backing bullpen ERA
# (7d). A final game's boxscore is immutable, so it's fetched once and reused
# across runs; see fetchers/mlb.build_game_entities.
BOXSCORE_CACHE_PATH = "data/boxscores.json"
# Only the top-N players by pulse score get insights (AI calls, store entries,
# and rendered cards). Caps generation/CI/merge work and keeps the committed
# store bounded (stale entries below the cap are pruned each generation run).
TOP_N = 20
# Simple interpretation task -> default to a small, fast, cheap model. Uses the
# CLI's short model alias ("haiku"); the fully-qualified id is not accepted by
# --model and hangs. Override with SP_INSIGHTS_MODEL=sonnet for richer prose.
MODEL = os.environ.get("SP_INSIGHTS_MODEL", "haiku")
# Pinned per-call timeout. The suggested 30s proved too tight in practice --
# real interpretation calls were observed at ~25s (CLI spawn + cold model +
# output), so a timeout would needlessly drop an entity's insight. 60s gives
# margin; a genuinely stuck call still can't hang the pipeline.
CALL_TIMEOUT_S = 60

# Auth safety: by default we STRIP these from the subprocess env so `claude`
# authenticates via the logged-in Claude *subscription* session, never against
# paid API billing. Opt into API-key auth deliberately with SP_ALLOW_API_BILLING=1.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

PROMPT_TEMPLATE = """You are a baseball analyst writing brief, factual context for a "who's hot" dashboard. You are given ONE player's already-computed recent stats as JSON. Your job is INTERPRETATION ONLY.

Hard rules:
- Never compute, estimate, round, or invent any number. Use only numbers that appear verbatim in the input. If unsure, omit the number.
- Never predict outcomes, games, or future performance. No "will", no odds, no projections. Describe what has happened and why it's notable.
- Plain language, no hype, no cliches.

Return STRICT JSON only (no prose, no markdown), exactly:
{ "story": "<2-3 sentences>", "summary": "<1 sentence>", "takeaways": ["<short>", "<short>", "<short>"] }

Player JSON:
"""

PROMPT_TEMPLATE_GAME = """You are a baseball analyst writing brief, factual context for one of today's games on a "who's hot" dashboard. You are given ONE game's already-computed matchup context as JSON. Your job is INTERPRETATION ONLY.

Hard rules:
- Never compute, estimate, round, or invent any number. Use only numbers that appear verbatim in the input. If unsure, omit the number.
- Never predict the outcome, score, or winner. No "will", no odds, no projections. Describe the form and context each side brings in, and why it's notable.
- Both teams' numbers are provided; write even-handed context, not a pick.
- Plain language, no hype, no cliches.

Return STRICT JSON only (no prose, no markdown), exactly:
{ "story": "<2-3 sentences>", "summary": "<1 sentence>" }

Game JSON:
"""


# ---------------- entity builder (deterministic; "AI never calculates") ----------------

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
    score = max(30, min(100, 100 - (best_rank - 1) * 7))
    label = ("Scorching" if score >= 85 else "Hot" if score >= 70 else "Warm" if score >= 55 else "Notable")
    return {"score": score, "label": label}


def build_entities(data):
    """Collapse the leaderboard (a player may appear in several categories) into
    one entity per player, with deterministic signals + pulse + a compact stats
    block. Returns {key: entity}."""
    entities = {}
    for sport in data.get("sports", {}).values():
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
                        "entity": p.get("entity"),
                        "team": p.get("team"),
                        "team_abbr": p.get("team_abbr"),
                        "position": p.get("position"),
                        "signals": [],
                        "stats": [],
                        "best_rank": p.get("rank") or 99,
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
                # newest game across appearances (ISO YYYY-MM-DD sorts lexically)
                lgd = p.get("last_game_date")
                if lgd and (ent["last_game_date"] is None or lgd > ent["last_game_date"]):
                    ent["last_game_date"] = lgd
                if ent.get("vs_next_starter") is None and p.get("vs_next_starter"):
                    ent["vs_next_starter"] = p.get("vs_next_starter")
    for ent in entities.values():
        ent["pulse"] = _pulse(ent["best_rank"])
    return entities


def _prompt_payload(ent):
    """The trimmed entity object sent to the model (no internal/display-only keys)."""
    payload = {
        "name": ent.get("entity"),
        "team": ent.get("team"),
        "position": ent.get("position"),
        "pulse": ent.get("pulse"),
        "signals": ent.get("signals"),
        "stats": ent.get("stats"),
    }
    if ent.get("vs_next_starter"):
        payload["vs_next_starter"] = ent["vs_next_starter"]
    return payload


# ---------------- Claude headless invocation ----------------

def _parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class _AuthError(RuntimeError):
    """Auth could not be established via the subscription session."""


def _subprocess_env():
    """Env for the `claude` subprocess. By default removes any API-key vars so
    the CLI must use the logged-in Claude subscription session -- this guarantees
    we never silently run against paid API billing. SP_ALLOW_API_BILLING=1 is an
    explicit opt-in to keep the key."""
    env = dict(os.environ)
    present = [v for v in API_KEY_VARS if env.get(v)]
    if present and not os.environ.get("SP_ALLOW_API_BILLING"):
        for v in present:
            env.pop(v, None)
        print("insights: NOTE {} set in env -> stripped so calls use your Claude "
              "subscription, not paid API billing (set SP_ALLOW_API_BILLING=1 to override)."
              .format(", ".join(present)))
    elif present:
        print("insights: SP_ALLOW_API_BILLING=1 -> using API-key auth; this MAY incur API charges.")
    return env


def _preflight(env):
    """One tiny call to confirm the session authenticates before the loop. Raises
    _AuthError (loud abort) rather than letting the run limp on -- and because the
    API key is already stripped, a failure here means only an API key was
    available, which we refuse to use silently."""
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", MODEL],
        input="Reply with exactly: ok", capture_output=True, text=True,
        timeout=CALL_TIMEOUT_S, env=env,
    )
    if proc.returncode != 0:
        raise _AuthError((proc.stderr or proc.stdout or "").strip()[:300])
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise _AuthError("unparseable preflight response")
    if envelope.get("is_error"):
        raise _AuthError("claude reported error: {}".format(envelope.get("subtype")))


def _invoke_claude(prompt, env):
    """Run one `claude -p` headless call and return the parsed JSON object from
    its result envelope. Raises on non-zero exit / error envelope / unparseable
    output (caller decides carry-forward). Shared by the player and game paths."""
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", MODEL],
        input=prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError("claude exit {}: {}".format(proc.returncode, (proc.stderr or "")[:200]))
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError("claude reported error: {}".format(envelope.get("subtype")))
    return _parse_json_object(envelope.get("result", ""))


def _call_claude(ent, env):
    """One headless call for one player entity. Returns {story,summary,takeaways}
    or raises on any failure (caller decides carry-forward)."""
    obj = _invoke_claude(PROMPT_TEMPLATE + json.dumps(_prompt_payload(ent), ensure_ascii=False), env)
    takeaways = obj.get("takeaways") or []
    if not isinstance(takeaways, list):
        takeaways = [str(takeaways)]
    return {
        "story": str(obj.get("story", "")).strip(),
        "summary": str(obj.get("summary", "")).strip(),
        "takeaways": [str(t).strip() for t in takeaways][:3],
    }


def _game_prompt_payload(ent):
    """The trimmed game object sent to the model. Includes BOTH sides' full
    context (framing surfaces one side per signal; the AI gets everything)."""
    return {
        "matchup": "{} @ {}".format((ent.get("away") or {}).get("name"), (ent.get("home") or {}).get("name")),
        "start": ent.get("start"),
        "venue": ent.get("venue"),
        "status": ent.get("status"),
        "probables": ent.get("probables"),
        "pulse": ent.get("pulse"),
        "context": ent.get("context"),
    }


def _call_claude_game(ent, env):
    """One headless call for one game entity. Returns {story,summary} (games carry
    no takeaways) or raises on any failure (caller decides carry-forward)."""
    obj = _invoke_claude(PROMPT_TEMPLATE_GAME + json.dumps(_game_prompt_payload(ent), ensure_ascii=False), env)
    return {"story": str(obj.get("story", "")).strip(), "summary": str(obj.get("summary", "")).strip()}


# ---------------- store I/O ----------------

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


def _needs_regen(ent, prev):
    if prev is None:
        return True
    if prev.get("template_version") != PROMPT_VERSION:
        return True
    cur, old = ent.get("last_game_date"), prev.get("last_game_date")
    if cur and (old is None or cur > old):  # a newer game has been played
        return True
    return False


def _needs_regen_game(ent, prev):
    """Games regenerate every run UNTIL they're final. A completed game's data
    never changes, so once the store has it as Final it's carried forever; a
    not-yet-final game (probables/form still moving) is refreshed each run. New
    games and prompt-template bumps always regenerate."""
    if prev is None:
        return True
    if prev.get("template_version") != GAME_PROMPT_VERSION:
        return True
    return prev.get("status") != "Final"


# ---------------- entry point ----------------

def _carry(prev):
    """Carry-forward view of a stored player record (or None if we have nothing)."""
    if not prev:
        return None
    return {"story": prev.get("story"), "summary": prev.get("summary"),
            "takeaways": prev.get("takeaways", [])}


def _carry_game(prev):
    """Carry-forward view of a stored game record (or None if we have nothing)."""
    if not prev:
        return None
    return {"story": prev.get("story"), "summary": prev.get("summary")}


def _top_n(entities, n):
    """The top-N entities by pulse score (desc), name as a deterministic
    tiebreak so the cap doesn't flap between runs on pulse ties."""
    def score(ent):
        return (ent.get("pulse") or {}).get("score", 0)
    ordered = sorted(entities.items(), key=lambda kv: (-score(kv[1]), (kv[1].get("entity") or "")))
    return dict(ordered[:n])


def _print_auth_abort(e):
    print("\n" + "!" * 70)
    print("insights: AUTH PREFLIGHT FAILED -- making no AI calls this run.")
    print("  reason: {}".format(str(e)[:220]))
    print("  This step runs ONLY on your Claude subscription session. If just an")
    print("  API key is available, log in with `claude`, or deliberately opt into")
    print("  API billing with SP_ALLOW_API_BILLING=1.")
    print("  -> merging committed insights only (no generation this run).")
    print("!" * 70 + "\n")


def _build_game_entities(config, generated_at):
    """Deterministic game builder + its stores. Runs in CI too (only the AI calls
    are gated later). Guarded: any failure yields empty games (players unaffected).
    Returns (game_entities, games_store, game_date)."""
    if config is None:
        return {}, {}, None
    game_date = generated_at.date().isoformat()
    try:
        box_cache = _load_store(BOXSCORE_CACHE_PATH)
        game_entities, box_cache = mlb.build_game_entities(config, game_date, box_cache)
        _save_store(BOXSCORE_CACHE_PATH, box_cache)
        games_store = _load_store(GAMES_STORE_PATH)
        print("insights(games): built {} games for {} (boxscore cache: {} final games)"
              .format(len(game_entities), game_date, len(box_cache)))
        return game_entities, games_store, game_date
    except Exception as e:  # noqa: BLE001 -- never let the games path break the pipeline
        print("insights(games): builder failed ({}); games section skipped".format(str(e)[:160]))
        return {}, {}, game_date


def run(data, generated_at, config=None, store_path=STORE_PATH):
    """Enrich `data` with per-player AND per-game insight, return a Markdown addendum.

    The MERGE (writing committed insight text into `data` -- the per-row `insight`
    objects and the card-ready `data["insights"]["players"|"games"]` sections)
    ALWAYS happens. Only the AI *generation* is skipped when `claude` is
    unavailable or SP_SKIP_INSIGHTS is set -- that's what lets deployed builds
    (which never have Claude) still surface the committed insights. The
    deterministic game builder itself runs even in CI; only its AI calls are gated.
    A single auth preflight covers both players and games."""
    now_iso = generated_at.isoformat()
    all_entities = build_entities(data)
    entities = _top_n(all_entities, TOP_N)  # cap players to top-N by pulse (games are NOT capped)
    store = _load_store(store_path)
    total = len(entities)

    # Games: full slate, uncapped. Built deterministically here (CI included).
    game_entities, games_store, _game_date = _build_game_entities(config, generated_at)

    skip_generation = bool(os.environ.get("SP_SKIP_INSIGHTS")) or shutil.which("claude") is None
    if skip_generation:
        reason = "SP_SKIP_INSIGHTS set" if os.environ.get("SP_SKIP_INSIGHTS") else "`claude` CLI not found"
        with_text = sum(1 for k in entities if (store.get(k) or {}).get("summary"))
        g_with_text = sum(1 for pk in game_entities if (games_store.get(pk) or {}).get("summary"))
        print("insights: {} -> merge-only: top {} players ({} w/ text), {} games ({} w/ text), no AI calls"
              .format(reason, total, with_text, len(game_entities), g_with_text))
        insight_map = {k: _carry(store.get(k)) for k in entities}
        game_text = {pk: _carry_game(games_store.get(pk)) for pk in game_entities}
    else:
        # One preflight for the whole run, iff anything (player or game) needs regen.
        needs = (any(_needs_regen(e, store.get(k)) for k, e in entities.items())
                 or any(_needs_regen_game(e, games_store.get(pk)) for pk, e in game_entities.items()))
        child_env, auth_ok = None, True
        if needs:
            child_env = _subprocess_env()
            try:
                _preflight(child_env)
            except Exception as e:  # noqa: BLE001 -- loud abort, never fall back to paid billing
                _print_auth_abort(e)
                auth_ok = False
        if not auth_ok:
            insight_map = {k: _carry(store.get(k)) for k in entities}
            game_text = {pk: _carry_game(games_store.get(pk)) for pk in game_entities}
        else:
            print("insights: top {} of {} players; {} games; model={}"
                  .format(total, len(all_entities), len(game_entities), MODEL))
            insight_map = _generate_all(entities, store, now_iso, total, store_path, child_env)
            game_text = (_generate_games(game_entities, games_store, now_iso, GAMES_STORE_PATH, child_env)
                         if game_entities else {})

    _write_back(data, insight_map)
    data["insights"] = _build_players_section(entities, insight_map, generated_at)
    if game_entities:
        data["insights"]["games"] = _build_games_section(game_entities, game_text)
    return _markdown_addendum(data, insight_map)


def _generate_all(entities, store, now_iso, total, store_path, child_env):
    """Run the player AI generation loop (regenerate changed entities, carry the
    rest). Returns the insight_map. Auth preflight is handled once by run(), so
    child_env is already validated (or None when nothing needs regen)."""
    gen = carried = failed = 0
    insight_map = {}
    new_store = {}  # rebuilt from the current (top-N) entity set -> prunes stale entries
    for i, (key, ent) in enumerate(sorted(entities.items()), start=1):
        prev = store.get(key)
        if _needs_regen(ent, prev):
            print("  [{}/{}] {} -- regenerating".format(i, total, ent.get("entity")))
            try:
                text = _call_claude(ent, child_env)
                gen += 1
            except Exception as e:  # noqa: BLE001 -- never let one entity break the run
                print("      call failed ({}); {}".format(
                    str(e)[:120], "carrying previous" if prev else "leaving empty"))
                failed += 1
                text = _carry(prev) or {"story": None, "summary": None, "takeaways": []}
        else:
            print("  [{}/{}] {} -- cached".format(i, total, ent.get("entity")))
            carried += 1
            text = _carry(prev)

        new_store[key] = {
            "entity": ent.get("entity"), "team": ent.get("team"),
            "last_game_date": ent.get("last_game_date"),
            "template_version": PROMPT_VERSION,
            "generated_at": now_iso if prev is None or _needs_regen(ent, prev) else prev.get("generated_at", now_iso),
            "story": text["story"], "summary": text["summary"], "takeaways": text["takeaways"],
        }
        insight_map[key] = {"story": text["story"], "summary": text["summary"], "takeaways": text["takeaways"]}

    _save_store(store_path, new_store)
    print("insights: generated {}, carried forward {}, failed {} (store pruned to {} entries)"
          .format(gen, carried, failed, len(new_store)))
    return insight_map


def _build_players_section(entities, insight_map, generated_at):
    """Card-ready player insights for the UI (data["insights"]["players"]).
    Reuses the deterministic pulse + signals from build_entities; story/summary
    come from insight_map. Sorted most-notable first. Always emitted -- in
    merge-only mode entities without committed text still appear (pulse/signals
    render; the AI block is simply omitted client-side when empty)."""
    players = []
    for key, ent in entities.items():
        ins = insight_map.get(key) or {}
        players.append({
            "name": ent.get("entity"),
            "team": ent.get("team_abbr"),
            "pos": ent.get("position"),
            "pulse": ent.get("pulse"),
            "signals": ent.get("signals"),
            "summary": ins.get("summary"),
            "story": ins.get("story"),
        })
    players.sort(key=lambda p: (p.get("pulse") or {}).get("score", 0), reverse=True)
    return {"generated_at": generated_at.isoformat(), "players": players}


def _generate_games(entities, store, now_iso, store_path, child_env):
    """Game AI generation loop: regenerate non-final games each run, carry final
    ones. Mirrors _generate_all -- rebuilds the store pruned to today's gamePks
    (yesterday's slate drops off). Returns {gamePk: {story, summary}}."""
    gen = carried = failed = 0
    text_map = {}
    new_store = {}  # rebuilt from today's slate -> prunes yesterday's games
    total = len(entities)
    for i, (pk, ent) in enumerate(entities.items(), start=1):
        prev = store.get(pk)
        label = "{} @ {}".format((ent.get("away") or {}).get("abbr"), (ent.get("home") or {}).get("abbr"))
        if _needs_regen_game(ent, prev):
            print("  [game {}/{}] {} -- regenerating".format(i, total, label))
            try:
                text = _call_claude_game(ent, child_env)
                gen += 1
            except Exception as e:  # noqa: BLE001 -- never let one game break the run
                print("      call failed ({}); {}".format(
                    str(e)[:120], "carrying previous" if prev else "leaving empty"))
                failed += 1
                text = _carry_game(prev) or {"story": None, "summary": None}
        else:
            print("  [game {}/{}] {} -- cached (final)".format(i, total, label))
            carried += 1
            text = _carry_game(prev)

        new_store[pk] = {
            "away": ent.get("away"), "home": ent.get("home"),
            "start": ent.get("start"), "venue": ent.get("venue"),
            "probables": ent.get("probables"), "signals": ent.get("signals"),
            "pulse": ent.get("pulse"), "betting_signals": ent.get("betting_signals"),
            "status": ent.get("status"),
            "template_version": GAME_PROMPT_VERSION,
            "generated_at": now_iso if (prev is None or _needs_regen_game(ent, prev)) else prev.get("generated_at", now_iso),
            "story": (text or {}).get("story"), "summary": (text or {}).get("summary"),
        }
        text_map[pk] = {"story": (text or {}).get("story"), "summary": (text or {}).get("summary")}

    _save_store(store_path, new_store)
    print("insights(games): generated {}, carried forward {}, failed {} (store pruned to {} entries)"
          .format(gen, carried, failed, len(new_store)))
    return text_map


def _build_games_section(entities, text_map):
    """Card-ready game insights for the UI, in slate order (no ranking/cap).
    Returns a bare list assigned to data["insights"]["games"] -- mirroring
    data["insights"]["players"] (also a bare list). Deterministic signals/pulse
    from the builder; story/summary from text_map (omitted client-side when empty)."""
    games = []
    for pk, ent in entities.items():
        t = text_map.get(pk) or {}
        games.append({
            "id": ent.get("gamePk"),
            "away": ent.get("away"),
            "home": ent.get("home"),
            "start": ent.get("start"),
            "venue": ent.get("venue"),
            "probables": ent.get("probables"),
            "pulse": ent.get("pulse"),
            "signals": ent.get("signals"),
            "betting_signals": ent.get("betting_signals"),
            "summary": t.get("summary"),
            "story": t.get("story"),
        })
    return games


def _write_back(data, insight_map):
    """Attach each player's insight onto every leaderboard row for that player."""
    for sport in data.get("sports", {}).values():
        for cat in sport.get("categories", []):
            for p in cat.get("players", []):
                key = _entity_key(p.get("entity"), p.get("team_abbr"))
                p["insight"] = insight_map.get(key)


def _markdown_addendum(data, insight_map):
    """A compact '## Insights' section listing each player's one-line summary."""
    seen, lines = set(), []
    for sport in data.get("sports", {}).values():
        for cat in sport.get("categories", []):
            for p in cat.get("players", []):
                key = _entity_key(p.get("entity"), p.get("team_abbr"))
                if key in seen:
                    continue
                seen.add(key)
                ins = insight_map.get(key) or {}
                if ins.get("summary"):
                    lines.append("- **{}** ({}): {}".format(
                        p.get("entity"), p.get("team_abbr") or p.get("team") or "-", ins["summary"]))
    if not lines:
        return ""
    return "\n## Insights\n\n" + "\n".join(lines) + "\n"
