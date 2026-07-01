#!/usr/bin/env python3
"""
AgentEgo Sentiment Worker
Run with: /home/cflux/ComfyUI/venv/bin/python /mnt/LargeStorage/AgentEgo/sentiment_worker.py

Polls AgentEgo every 60s for sessions/rounds needing scoring. The scoring backend is
config-driven (control panel):
  * "llm"        — one combined local-LLM (Ollama) call per round returns user + agent
                   emotions over a configurable taxonomy AND direct mood scores.
  * "goemotions" — the original SamLowe/roberta-base-go_emotions model (lazy-loaded).
"""

import sys
import time
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sentiment-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GOEMOTIONS_MODEL = "SamLowe/roberta-base-go_emotions"
EGO_URL = "http://localhost:8765"
POLL_INTERVAL = 60  # seconds
MSG_TRUNC = 600     # chars per message in the LLM transcript

_goemotions = None  # lazily loaded classifier (only for the goemotions backend)


# ---------------------------------------------------------------- GoEmotions backend
def load_goemotions():
    global _goemotions
    if _goemotions is not None:
        return _goemotions
    try:
        from transformers import pipeline
        import torch
        device = 0 if torch.cuda.is_available() else -1
        device_name = torch.cuda.get_device_name(0) if device == 0 else "CPU"
        log.info("Loading GoEmotions model %s on %s ...", GOEMOTIONS_MODEL, device_name)
        _goemotions = pipeline("text-classification", model=GOEMOTIONS_MODEL, top_k=None, device=device)
        log.info("GoEmotions model loaded.")
        return _goemotions
    except ImportError:
        log.error("transformers not found. Run with: /home/cflux/ComfyUI/venv/bin/python")
        sys.exit(1)


def score_goemotions(texts):
    if not texts:
        return None
    try:
        results = load_goemotions()(texts, truncation=True, max_length=512)
    except Exception as e:
        log.warning("GoEmotions inference error: %s", e)
        return None
    totals = {}
    for msg_scores in results:
        for s in msg_scores:
            totals[s["label"]] = totals.get(s["label"], 0) + s["score"]
    avg = {k: round(v / len(texts), 4) for k, v in totals.items()}
    ranked = sorted(avg, key=avg.get, reverse=True)
    return {"dominant": ranked[0], "top3": ranked[:3], "scores": avg, "message_count": len(texts)}


# ---------------------------------------------------------------- LLM backend
def call_ollama(url, model, system, user):
    r = requests.post(f"{url}/api/chat", json={
        "model": model, "format": "json", "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, timeout=240)
    r.raise_for_status()
    import json
    return json.loads(r.json()["message"]["content"])


def _emotion_score(raw_scores, taxonomy, msg_count):
    """LLM 0-10 emotion dict -> the stored shape (scores normalized to 0-1, dominant, top3)."""
    valid = {}
    tax = set(taxonomy)
    for k, v in (raw_scores or {}).items():
        k = str(k).strip().lower()
        if k in tax:
            try:
                valid[k] = round(min(10.0, max(0.0, float(v))) / 10.0, 4)
            except (TypeError, ValueError):
                pass
    if not valid or msg_count == 0:
        return None
    ranked = sorted(valid, key=valid.get, reverse=True)
    return {"dominant": ranked[0], "top3": ranked[:3], "scores": valid, "message_count": msg_count}


def score_llm_round(msgs, cfg):
    """Returns (user_score, agent_score, mood_scores) from one combined LLM call."""
    taxonomy = cfg.get("taxonomy") or []
    moods = cfg.get("moods") or []
    mood_ids = {m["id"] for m in moods}
    user_n = sum(1 for m in msgs if m["role"] == "user")
    agent_n = sum(1 for m in msgs if m["role"] == "assistant")
    transcript = "\n".join(f'{m["role"]}: {m["content"][:MSG_TRUNC]}' for m in msgs) or "(empty)"

    mood_lines = "\n".join(f'  "{m["id"]}" = {m["name"]}: {m.get("description") or ""}' for m in moods)
    labels = ", ".join(taxonomy)
    system = (
        "You analyze a short chat excerpt between a USER and an AI AGENT. First rate the emotions "
        "expressed by each, then rate the AGENT's mood. Use ONLY the provided emotion labels and "
        "mood ids. Score everything 0-10 (0=absent, 10=strong). Be decisive — most values are 0. "
        'Return ONLY JSON with keys: {"emotions_user": {emotion: 0-10}, '
        '"emotions_agent": {emotion: 0-10}, "moods": {mood_id: 0-10}}.'
    )
    user = (f"EMOTION LABELS: {labels}\n\nMOOD CATALOG:\n{mood_lines}\n\nCHAT EXCERPT:\n{transcript}\n\n"
            "Return emotions_user, emotions_agent, and moods.")
    data = call_ollama(cfg["llm_url"], cfg["llm_model"], system, user)

    user_score = _emotion_score(data.get("emotions_user"), taxonomy, user_n)
    agent_score = _emotion_score(data.get("emotions_agent"), taxonomy, agent_n)
    mood_scores = {}
    for k, v in (data.get("moods") or {}).items():
        if k in mood_ids:
            try:
                mood_scores[k] = int(round(min(10.0, max(0.0, float(v)))))
            except (TypeError, ValueError):
                pass
    return user_score, agent_score, mood_scores


# ---------------------------------------------------------------- shared plumbing
def get_scoring_config():
    try:
        return requests.get(f"{EGO_URL}/api/sentiment/scoring-config", timeout=10).json()
    except Exception as e:
        log.warning("Could not fetch scoring-config (defaulting to goemotions): %s", e)
        return {"backend": "goemotions"}


def check_trigger():
    try:
        status = requests.get(f"{EGO_URL}/api/sentiment/status", timeout=5).json()
        if status.get("triggered"):
            requests.post(f"{EGO_URL}/api/sentiment/trigger-clear", timeout=5)
            return True
    except Exception:
        pass
    return False


def heartbeat():
    try:
        requests.post(f"{EGO_URL}/api/sentiment/heartbeat", timeout=5)
    except Exception:
        pass


def report_progress(current, total, session_id=""):
    try:
        requests.post(f"{EGO_URL}/api/sentiment/progress",
                      params={"current": current, "total": total, "session_id": session_id}, timeout=5)
    except Exception:
        pass


def process_pending():
    heartbeat()
    cfg = get_scoring_config()
    backend = cfg.get("backend", "goemotions")

    try:
        resp = requests.get(f"{EGO_URL}/api/sentiment/pending", timeout=10)
        resp.raise_for_status()
        pending = resp.json()
    except Exception as e:
        log.warning("Could not reach AgentEgo: %s", e)
        return

    if not pending:
        return

    log.info("%d item(s) pending scoring (backend=%s)", len(pending), backend)
    total = len(pending)

    for idx, sid in enumerate(pending, start=1):
        report_progress(idx, total, sid)
        try:
            msgs = requests.get(f"{EGO_URL}/api/sessions/{sid}/messages", timeout=10).json()
        except Exception as e:
            log.warning("Could not fetch messages for %s: %s", sid, e)
            continue

        mood_scores = None
        if backend == "llm":
            try:
                user_score, agent_score, mood_scores = score_llm_round(msgs, cfg)
            except Exception as e:
                log.warning("LLM scoring failed for %s: %s", sid, e)
                continue
        else:
            user_score = score_goemotions([m["content"] for m in msgs if m["role"] == "user"])
            agent_score = score_goemotions([m["content"] for m in msgs if m["role"] == "assistant"])

        if not user_score and not agent_score:
            requests.post(f"{EGO_URL}/api/sentiment/score",
                          json={"session_id": sid, "user": None, "agent": None}, timeout=10)
            continue

        payload = {"session_id": sid, "user": user_score, "agent": agent_score}
        if mood_scores is not None:
            payload["mood_scores"] = mood_scores
        try:
            requests.post(f"{EGO_URL}/api/sentiment/score", json=payload, timeout=15)
            du = user_score["dominant"] if user_score else "—"
            da = agent_score["dominant"] if agent_score else "—"
            extra = f" | moods: {sorted(mood_scores, key=mood_scores.get, reverse=True)[:3]}" if mood_scores else ""
            log.info("Scored %s | user: %s | agent: %s%s", sid[:24], du, da, extra)
        except Exception as e:
            log.warning("Failed to post score for %s: %s", sid, e)

    report_progress(0, 0)
    try:
        requests.post(f"{EGO_URL}/api/sentiment/complete", timeout=5)
    except Exception:
        pass


def run():
    while True:
        process_pending()
        for _ in range(POLL_INTERVAL // 5):
            time.sleep(5)
            if check_trigger():
                log.info("Triggered by UI — running scoring now")
                break


if __name__ == "__main__":
    log.info("Worker started. Polling %s every %ds (backend is config-driven)", EGO_URL, POLL_INTERVAL)
    run()
