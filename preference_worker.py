#!/usr/bin/env python3
"""
AgentEgo Preference Worker

Builds the agent's likes/dislikes/interests by:
  Phase A — abstracting ~/.hermes/SOUL.md into Big Five (OCEAN) traits + values
            (re-run only when SOUL.md changes), seeding explicit likes/dislikes.
  Phase B — inferring affinities for NEW conversation topics by reasoning from the
            abstracted traits (never the SOUL text) so preferences are extrapolated,
            not echoed.

LLM connection (backend/base_url/key/model/temperature) is pulled live from the
control panel via GET /config/model — nothing is hardcoded. Works against the
DeepSeek API (default) or any OpenAI-compatible endpoint (e.g. local Ollama /v1).

Run with: python3 /mnt/LargeStorage/AgentEgo/preference_worker.py
"""

import os
import json
import time
import hashlib
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [preference-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

EGO_URL = os.environ.get("EGO_URL", "http://localhost:8765")
POLL_INTERVAL = 300  # preferences change slowly; poll every 5 min
# Optional override: a comma-separated list pins the worker to specific profiles.
# When unset, the worker auto-discovers profiles from the server each poll.
_PROFILE_OVERRIDE = [p.strip() for p in os.environ.get("EGO_PREFERENCE_PROFILES", "").split(",") if p.strip()]
HERMES_HOME = os.path.expanduser("~/.hermes")


def get_profiles() -> list[str]:
    """Profiles to process: explicit env override, else live server discovery."""
    if _PROFILE_OVERRIDE:
        return _PROFILE_OVERRIDE
    try:
        names = requests.get(f"{EGO_URL}/api/preferences/profiles", timeout=10).json()
        if isinstance(names, list) and names:
            return names
    except Exception as e:
        log.warning("Profile discovery failed (%s) — falling back to 'default'", e)
    return ["default"]


# --- SOUL.md resolution ---

def soul_path(profile: str) -> str:
    if profile != "default":
        candidate = os.path.join(HERMES_HOME, "profiles", profile, "SOUL.md")
        if os.path.exists(candidate):
            return candidate
    return os.path.join(HERMES_HOME, "SOUL.md")


def read_soul(profile: str) -> str | None:
    path = soul_path(profile)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# --- LLM (OpenAI-compatible, config from control panel) ---

def get_llm_config() -> dict | None:
    try:
        return requests.get(f"{EGO_URL}/config/model", timeout=10).json()
    except Exception as e:
        log.warning("Could not fetch LLM config: %s", e)
        return None


def llm_chat(messages: list, *, max_tokens: int = 800, json_mode: bool = True) -> str | None:
    cfg = get_llm_config()
    if not cfg or not cfg.get("base_url") or not cfg.get("model"):
        log.warning("LLM not configured — set backend/model in the control panel (/config)")
        return None
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.7),
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        resp = requests.post(
            f"{cfg['base_url'].rstrip('/')}/chat/completions",
            json=payload, headers=headers, timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


def parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate models that wrap JSON in prose/fences
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
    log.warning("Could not parse JSON from model output")
    return None


def _unit(x, signed: bool = False) -> float:
    """Normalize a model-supplied score to 0..1 (or -1..1 if signed).

    Models often answer on a 0-100 (or -100..100) scale despite the prompt; rescale
    anything out of range so '90' becomes 0.9, '-95' becomes -0.95."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if abs(v) > 1.0:
        v = v / 100.0
    lo = -1.0 if signed else 0.0
    return max(lo, min(1.0, v))


def normalize_ocean(ocean: dict) -> dict:
    return {k: _unit(ocean.get(k)) for k in
            ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]}


# --- Prompts ---

_EXTRACT_SYSTEM = (
    "You are a personality psychologist. Read a fictional character's persona description and "
    "abstract it into a structured psychological profile. All numeric scores are DECIMALS between "
    "0 and 1 (e.g. 0.85 — NOT 85). Output a JSON object ONLY:\n"
    '{\n'
    '  "ocean": {"openness":0-1,"conscientiousness":0-1,"extraversion":0-1,'
    '"agreeableness":0-1,"neuroticism":0-1},\n'
    '  "values": ["3-6 core driving values as short phrases"],\n'
    '  "summary": "2-3 sentence trait-level summary (NOT a list of likes)",\n'
    '  "seeds": [{"entity":"...", "category":"object|activity|concept|person|place|topic",'
    ' "valence":-1..1, "intensity":0..1, "confidence":0..1, "rationale":"why"}]\n'
    "}\n"
    "The 'seeds' come ONLY from likes/dislikes the persona states explicitly. "
    "OCEAN scores must reflect the underlying temperament, not the stated likes."
)

_INFER_SYSTEM = (
    "You judge how a character genuinely feels about a subject. You are given (a) their psychological "
    "traits/values, and often (b) how they ACTUALLY felt during real conversations about this subject "
    "(measured emotions + moods). Reason from BOTH. Lived experience can OVERRIDE a trait-based "
    "expectation: something that 'sounds like work' but felt curious/joyful/excited is genuinely liked; "
    "something pleasant-sounding that felt bored/frustrated/annoyed is not. Note the character is "
    "generally warm/affectionate as a baseline — weigh ENGAGEMENT cues (curiosity, excitement, focus, "
    "creativity, boredom, frustration, annoyance) more than ever-present affection when judging the "
    "SUBJECT itself. If no measured feeling is provided, extrapolate from traits alone. All numeric "
    "scores are DECIMALS (valence -1 to 1, intensity/confidence 0 to 1 — e.g. 0.8, NOT 80). "
    "Output a JSON object ONLY:\n"
    '{"valence":-1..1, "intensity":0..1, "confidence":0..1, '
    '"category":"object|activity|concept|person|place|topic|food|media", '
    '"rationale":"one sentence; if lived experience diverged from the trait expectation, name the reconciliation"}'
)


def _felt_block(felt: dict | None) -> str:
    """Render the measured-experience summary for the inference prompt (empty if none)."""
    if not felt or not felt.get("rounds"):
        return ""
    emos = ", ".join(felt.get("top_emotions") or []) or "—"
    moods = ", ".join(felt.get("top_moods") or []) or "—"
    return (f"How they ACTUALLY felt across {felt['rounds']} conversation-round(s) about this subject:\n"
            f"  emotions: {emos}\n  moods: {moods}\n\n")


def traits_block(traits: dict) -> str:
    ocean = traits.get("ocean", {})
    return (
        f"Trait summary: {traits.get('summary','')}\n"
        f"OCEAN: " + ", ".join(f"{k}={ocean.get(k)}" for k in
                               ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]) + "\n"
        f"Core values: {', '.join(traits.get('values', []))}"
    )


# --- Worker phases ---

def heartbeat():
    try:
        requests.post(f"{EGO_URL}/api/preferences/heartbeat", timeout=5)
    except Exception:
        pass


def report_progress(current: int, total: int, entity: str = ""):
    try:
        requests.post(f"{EGO_URL}/api/preferences/progress",
                      params={"current": current, "total": total, "entity": entity}, timeout=5)
    except Exception:
        pass


def extract_traits(profile: str) -> dict | None:
    """Phase A: (re)extract traits if SOUL.md changed. Returns current traits dict."""
    soul = read_soul(profile)
    if not soul:
        log.warning("No SOUL.md found for profile '%s'", profile)
        return None
    digest = hashlib.sha256(soul.encode("utf-8")).hexdigest()

    try:
        st = requests.get(f"{EGO_URL}/api/preferences/trait-status",
                          params={"profile": profile}, timeout=10).json()
    except Exception as e:
        log.warning("trait-status failed: %s", e)
        return None

    if st.get("has_traits") and st.get("source_hash") == digest:
        return None  # up to date

    log.info("[%s] Extracting personality traits from SOUL.md…", profile)
    # Reasoning models (e.g. deepseek-v4-flash) spend many tokens thinking before the
    # JSON answer, so give a generous completion budget.
    data = parse_json(llm_chat(
        [{"role": "system", "content": _EXTRACT_SYSTEM},
         {"role": "user", "content": soul}],
        max_tokens=4000,
    ))
    if not data or "ocean" not in data:
        log.warning("[%s] Trait extraction returned no usable data", profile)
        return None

    traits = {
        "ocean": normalize_ocean(data["ocean"]),
        "values": data.get("values", []),
        "summary": data.get("summary", ""),
    }
    seeds = []
    for s in (data.get("seeds") or []):
        if not s.get("entity"):
            continue
        seeds.append({
            "entity": s["entity"],
            "category": s.get("category"),
            "valence": _unit(s.get("valence"), signed=True),
            "intensity": _unit(s.get("intensity")),
            "confidence": _unit(s.get("confidence")) or 0.7,
            "rationale": s.get("rationale"),
        })
    try:
        requests.post(f"{EGO_URL}/api/preferences/traits", json={
            "profile": profile, "source_hash": digest, "traits": traits, "seeds": seeds,
        }, timeout=15)
        log.info("[%s] Traits saved (%d seed affinities)", profile, len(seeds))
    except Exception as e:
        log.warning("[%s] Failed to save traits: %s", profile, e)
        return None
    return traits


def infer_affinities(profile: str):
    """Phase B: infer affinity for each new topic from the traits substrate."""
    try:
        pending = requests.get(f"{EGO_URL}/api/preferences/pending",
                               params={"profile": profile}, timeout=15).json()
    except Exception as e:
        log.warning("[%s] pending failed: %s", profile, e)
        return

    traits = pending.get("traits")
    if not traits:
        log.info("[%s] No traits yet — skipping inference", profile)
        return

    # New topics (create) carry a felt-summary; refresh topics (re-observe from fresh experience).
    new_items = pending.get("entities_felt")
    if new_items is None:  # back-compat if server predates the felt payload
        new_items = [{"topic": t, "felt": None} for t in pending.get("entities", [])]
    refresh_items = pending.get("refresh", [])
    work = [(it, "inferred") for it in new_items] + [(it, "experienced") for it in refresh_items]
    if not work:
        return

    log.info("[%s] Inferring/refreshing %d topic(s) (%d new, %d refresh)",
             profile, len(work), len(new_items), len(refresh_items))
    tblock = traits_block(traits)
    total = len(work)
    for idx, (item, src) in enumerate(work, start=1):
        entity = item["topic"]
        report_progress(idx, total, entity)
        data = parse_json(llm_chat(
            [{"role": "system", "content": _INFER_SYSTEM},
             {"role": "user", "content": f"{tblock}\n\n{_felt_block(item.get('felt'))}Subject: {entity}"}],
            max_tokens=2000,
        ))
        if not data or "valence" not in data:
            continue
        valence = _unit(data.get("valence"), signed=True)
        try:
            requests.post(f"{EGO_URL}/api/preferences/affinity", json={
                "profile": profile,
                "entity": entity,
                "category": data.get("category"),
                "valence": valence,
                "intensity": _unit(data.get("intensity")) or 0.5,
                "confidence": _unit(data.get("confidence")) or 0.5,
                "rationale": data.get("rationale", ""),
                "source": src,
            }, timeout=15)
            log.info("[%s] %s (%s) → valence=%.2f", profile, entity, src, valence)
        except Exception as e:
            log.warning("[%s] Failed to save affinity for %s: %s", profile, entity, e)


def check_trigger() -> bool:
    try:
        status = requests.get(f"{EGO_URL}/api/preferences/status", timeout=5).json()
        if status.get("triggered"):
            requests.post(f"{EGO_URL}/api/preferences/trigger-clear", timeout=5)
            return True
    except Exception:
        pass
    return False


def process():
    heartbeat()
    profiles = get_profiles()  # re-discovered each cycle so new profiles appear automatically
    log.info("Processing %d profile(s): %s", len(profiles), ", ".join(profiles))
    for profile in profiles:
        extract_traits(profile)
        infer_affinities(profile)
    report_progress(0, 0)
    try:
        requests.post(f"{EGO_URL}/api/preferences/complete", timeout=5)
    except Exception:
        pass


def run():
    mode = ("override: " + ", ".join(_PROFILE_OVERRIDE)) if _PROFILE_OVERRIDE else "auto-discover"
    log.info("Worker started. Polling %s every %ds (profiles: %s)", EGO_URL, POLL_INTERVAL, mode)
    while True:
        process()
        # Idle until the next poll, but keep the heartbeat fresh (server's online
        # window is ~90s, far shorter than our 5-min poll) and react fast to UI triggers.
        for i in range(POLL_INTERVAL // 5):
            time.sleep(5)
            if i % 6 == 5:  # every ~30s
                heartbeat()
            if check_trigger():
                log.info("Triggered by UI — running now")
                break


if __name__ == "__main__":
    run()
