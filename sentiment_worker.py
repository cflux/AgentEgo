#!/usr/bin/env python3
"""
AgentEgo Sentiment Worker
Run with: /home/cflux/ComfyUI/venv/bin/python /mnt/LargeStorage/AgentEgo/sentiment_worker.py

Polls AgentEgo every 60s for sessions needing emotion scoring,
scores user and agent messages separately using the GoEmotions model,
and posts results back to AgentEgo.
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

MODEL = "SamLowe/roberta-base-go_emotions"
EGO_URL = "http://localhost:8765"
POLL_INTERVAL = 60  # seconds


def load_model():
    try:
        from transformers import pipeline
        import torch
        device = 0 if torch.cuda.is_available() else -1
        device_name = torch.cuda.get_device_name(0) if device == 0 else "CPU"
        log.info("Loading model %s on %s ...", MODEL, device_name)
        clf = pipeline("text-classification", model=MODEL, top_k=None, device=device)
        log.info("Model loaded.")
        return clf
    except ImportError:
        log.error("transformers not found. Run with: /home/cflux/ComfyUI/venv/bin/python")
        sys.exit(1)


def score_messages(classifier, texts: list[str]) -> dict | None:
    if not texts:
        return None
    try:
        results = classifier(texts, truncation=True, max_length=512)
    except Exception as e:
        log.warning("Inference error: %s", e)
        return None

    totals: dict[str, float] = {}
    for msg_scores in results:
        for s in msg_scores:
            totals[s["label"]] = totals.get(s["label"], 0) + s["score"]

    avg = {k: round(v / len(texts), 4) for k, v in totals.items()}
    sorted_emotions = sorted(avg, key=avg.get, reverse=True)
    return {
        "dominant": sorted_emotions[0],
        "top3": sorted_emotions[:3],
        "scores": avg,
        "message_count": len(texts),
    }


def check_trigger() -> bool:
    """Return True if the UI requested an immediate run, then clear the flag."""
    try:
        status = requests.get(f"{EGO_URL}/api/sentiment/status", timeout=5).json()
        if status.get("triggered"):
            # Clear the flag
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


def report_progress(current: int, total: int, session_id: str = ""):
    try:
        requests.post(
            f"{EGO_URL}/api/sentiment/progress",
            params={"current": current, "total": total, "session_id": session_id},
            timeout=5,
        )
    except Exception:
        pass


def process_pending(classifier):
    heartbeat()

    try:
        resp = requests.get(f"{EGO_URL}/api/sentiment/pending", timeout=10)
        resp.raise_for_status()
        pending = resp.json()
    except Exception as e:
        log.warning("Could not reach AgentEgo: %s", e)
        return

    if not pending:
        return

    log.info("%d session(s) pending scoring", len(pending))
    total = len(pending)

    for idx, session_id in enumerate(pending, start=1):
        report_progress(idx, total, session_id)

        try:
            msgs = requests.get(
                f"{EGO_URL}/api/sessions/{session_id}/messages", timeout=10
            ).json()
        except Exception as e:
            log.warning("Could not fetch messages for %s: %s", session_id, e)
            continue

        user_texts  = [m["content"] for m in msgs if m["role"] == "user"]
        agent_texts = [m["content"] for m in msgs if m["role"] == "assistant"]

        user_score  = score_messages(classifier, user_texts)
        agent_score = score_messages(classifier, agent_texts)

        if not user_score and not agent_score:
            log.info("No scoreable messages for session %s, skipping", session_id)
            requests.post(f"{EGO_URL}/api/sentiment/score", json={
                "session_id": session_id, "user": None, "agent": None,
            }, timeout=10)
            continue

        payload = {"session_id": session_id, "user": user_score, "agent": agent_score}
        try:
            requests.post(f"{EGO_URL}/api/sentiment/score", json=payload, timeout=10)
            dominant_u = user_score["dominant"]  if user_score  else "—"
            dominant_a = agent_score["dominant"] if agent_score else "—"
            log.info("Scored %s | user: %s | agent: %s", session_id[:24], dominant_u, dominant_a)
        except Exception as e:
            log.warning("Failed to post score for %s: %s", session_id, e)

    # Clear progress indicator and signal completion to the UI
    report_progress(0, 0)
    try:
        requests.post(f"{EGO_URL}/api/sentiment/complete", timeout=5)
    except Exception:
        pass


def run(classifier):
    while True:
        process_pending(classifier)
        # Sleep in 5s increments so we can react to UI trigger quickly
        for _ in range(POLL_INTERVAL // 5):
            time.sleep(5)
            if check_trigger():
                log.info("Triggered by UI — running scoring now")
                break


if __name__ == "__main__":
    clf = load_model()
    log.info("Worker started. Polling %s every %ds", EGO_URL, POLL_INTERVAL)
    run(clf)
