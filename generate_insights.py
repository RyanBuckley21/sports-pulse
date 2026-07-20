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

PROMPT_VERSION = "v1"
STORE_PATH = "data/insights.json"
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


def _call_claude(ent, env):
    """One headless call for one entity. Returns {story,summary,takeaways} or
    raises on any failure (caller decides carry-forward)."""
    prompt = PROMPT_TEMPLATE + json.dumps(_prompt_payload(ent), ensure_ascii=False)
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", MODEL],
        input=prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError("claude exit {}: {}".format(proc.returncode, (proc.stderr or "")[:200]))
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError("claude reported error: {}".format(envelope.get("subtype")))
    obj = _parse_json_object(envelope.get("result", ""))
    takeaways = obj.get("takeaways") or []
    if not isinstance(takeaways, list):
        takeaways = [str(takeaways)]
    return {
        "story": str(obj.get("story", "")).strip(),
        "summary": str(obj.get("summary", "")).strip(),
        "takeaways": [str(t).strip() for t in takeaways][:3],
    }


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


# ---------------- entry point ----------------

def run(data, generated_at, store_path=STORE_PATH):
    """Enrich `data` in place with per-player `insight`, maintain the committed
    store, and return a Markdown addendum (empty string when skipped)."""
    if os.environ.get("SP_SKIP_INSIGHTS"):
        print("insights: SP_SKIP_INSIGHTS set -> skipping AI step")
        return ""
    if shutil.which("claude") is None:
        print("insights: `claude` CLI not found -> skipping AI step (data pipeline unaffected)")
        return ""

    entities = build_entities(data)
    store = _load_store(store_path)
    total = len(entities)
    now_iso = generated_at.isoformat()
    print("insights: {} unique players; model={}".format(total, MODEL))

    # Only auth/preflight when at least one entity actually needs a model call,
    # so a fully-cached run stays truly zero-call.
    child_env = None
    if any(_needs_regen(e, store.get(k)) for k, e in entities.items()):
        child_env = _subprocess_env()
        try:
            _preflight(child_env)
        except Exception as e:  # noqa: BLE001 -- loud abort, never fall back to paid billing
            print("\n" + "!" * 70)
            print("insights: AUTH PREFLIGHT FAILED -- making no AI calls this run.")
            print("  reason: {}".format(str(e)[:220]))
            print("  This step runs ONLY on your Claude subscription session. If just an")
            print("  API key is available, log in with `claude`, or deliberately opt into")
            print("  API billing with SP_ALLOW_API_BILLING=1.")
            print("!" * 70 + "\n")
            return ""  # data pipeline already produced its output; simply no insights

    gen = carried = failed = 0
    insight_map = {}
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
                text = {"story": prev.get("story"), "summary": prev.get("summary"),
                        "takeaways": prev.get("takeaways", [])} if prev else \
                       {"story": None, "summary": None, "takeaways": []}
        else:
            print("  [{}/{}] {} -- cached".format(i, total, ent.get("entity")))
            carried += 1
            text = {"story": prev.get("story"), "summary": prev.get("summary"),
                    "takeaways": prev.get("takeaways", [])}

        store[key] = {
            "entity": ent.get("entity"), "team": ent.get("team"),
            "last_game_date": ent.get("last_game_date"),
            "template_version": PROMPT_VERSION,
            "generated_at": now_iso if prev is None or _needs_regen(ent, prev) else prev.get("generated_at", now_iso),
            "story": text["story"], "summary": text["summary"], "takeaways": text["takeaways"],
        }
        insight_map[key] = {"story": text["story"], "summary": text["summary"], "takeaways": text["takeaways"]}

    _save_store(store_path, store)
    print("insights: generated {}, carried forward {}, failed {}".format(gen, carried, failed))

    _write_back(data, insight_map)
    return _markdown_addendum(data, insight_map)


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
