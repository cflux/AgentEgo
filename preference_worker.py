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
PROFILES = [p.strip() for p in os.environ.get("EGO_PREFERENCE_PROFILES", "default").split(",") if p.strip()]
HERMES_HOME = os.path.expanduser("~/.hermes")


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


# --- Prompts ---

_EXTRACT_SYSTEM = (
    "You are a personality psychologist. Read a fictional character's persona description and "
    "abstract it into a structured psychological profile. Output a JSON object ONLY:\n"
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
    "You ARE a character defined ONLY by the psychological traits given below — you do NOT have "
    "access to any list of stated likes. Decide how this character would feel about a subject by "
    "extrapolating from these traits and values. The subject is likely NOT something in the "
    "character's background; reason about who they are. Output a JSON object ONLY:\n"
    '{"valence":-1..1, "intensity":0..1, "confidence":0..1, '
    '"category":"object|activity|concept|person|place|topic|food|media", '
    '"rationale":"one sentence grounded in the traits"}'
)


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
    data = parse_json(llm_chat(
        [{"role": "system", "content": _EXTRACT_SYSTEM},
         {"role": "user", "content": soul}],
        max_tokens=1200,
    ))
    if not data or "ocean" not in data:
        log.warning("[%s] Trait extraction returned no usable data", profile)
        return None

    traits = {"ocean": data["ocean"], "values": data.get("values", []), "summary": data.get("summary", "")}
    seeds = data.get("seeds", []) or []
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
    entities = pending.get("entities", [])
    if not traits:
        log.info("[%s] No traits yet — skipping inference", profile)
        return
    if not entities:
        return

    log.info("[%s] Inferring affinity for %d new topic(s)", profile, len(entities))
    tblock = traits_block(traits)
    total = len(entities)
    for idx, entity in enumerate(entities, start=1):
        report_progress(idx, total, entity)
        data = parse_json(llm_chat(
            [{"role": "system", "content": _INFER_SYSTEM},
             {"role": "user", "content": f"{tblock}\n\nSubject: {entity}"}],
            max_tokens=250,
        ))
        if not data or "valence" not in data:
            continue
        try:
            requests.post(f"{EGO_URL}/api/preferences/affinity", json={
                "profile": profile,
                "entity": entity,
                "category": data.get("category"),
                "valence": float(data.get("valence", 0.0)),
                "intensity": float(data.get("intensity", 0.5)),
                "confidence": float(data.get("confidence", 0.5)),
                "rationale": data.get("rationale", ""),
                "source": "inferred",
            }, timeout=15)
            log.info("[%s] %s → valence=%.2f", profile, entity, float(data.get("valence", 0.0)))
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
    for profile in PROFILES:
        extract_traits(profile)
        infer_affinities(profile)
    report_progress(0, 0)
    try:
        requests.post(f"{EGO_URL}/api/preferences/complete", timeout=5)
    except Exception:
        pass


def run():
    log.info("Worker started. Polling %s every %ds (profiles: %s)", EGO_URL, POLL_INTERVAL, ", ".join(PROFILES))
    while True:
        process()
        for _ in range(POLL_INTERVAL // 5):
            time.sleep(5)
            if check_trigger():
                log.info("Triggered by UI — running now")
                break


if __name__ == "__main__":
    run()
