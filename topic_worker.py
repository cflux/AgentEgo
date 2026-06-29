#!/usr/bin/env python3
"""
AgentEgo Session Analysis Worker
Polls AgentEgo for conversations needing labels, calls Ollama (CPU-only via num_gpu=0)
to generate a 1-3 word topic and conversation mode, and posts results back.

Run with: python /mnt/LargeStorage/AgentEgo/topic_worker.py
"""

import re
import time
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [topic-worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:1.5b"
EGO_URL = "http://localhost:8765"
POLL_INTERVAL = 60

VALID_MODES = {"work", "social", "informative", "serious", "flirting", "creative", "support"}

# Map common off-list words the model emits to a canonical mode.
_MODE_SYNONYMS = {
    "flirty": "flirting", "flirtatious": "flirting", "romantic": "flirting",
    "romance": "flirting", "intimate": "flirting", "dating": "flirting",
    "sexual": "flirting", "seductive": "flirting", "playful": "flirting",
    "casual": "social", "chat": "social", "chatting": "social", "greeting": "social",
    "friendly": "social", "smalltalk": "social", "conversation": "social", "social": "social",
    "coding": "work", "technical": "work", "debugging": "work", "development": "work",
    "programming": "work", "engineering": "work", "task": "work",
    "question": "informative", "informational": "informative", "educational": "informative",
    "learning": "informative", "explanation": "informative", "factual": "informative",
    "help": "support", "helping": "support", "assistance": "support",
    "troubleshooting": "support", "emotional": "support", "comfort": "support",
    "art": "creative", "writing": "creative", "design": "creative",
    "story": "creative", "brainstorm": "creative", "imaginative": "creative",
}


def _canon_mode(word: str) -> str | None:
    """Resolve a raw word to a canonical mode, via the valid set or synonyms."""
    if not word:
        return None
    if word in VALID_MODES:
        return word
    return _MODE_SYNONYMS.get(word)

_SYSTEM = (
    "Classify a conversation by its user messages.\n"
    "Output exactly TWO lines:\n"
    "Line 1: topic in 1-3 lowercase words\n"
    "Line 2: one mode: work, social, informative, serious, flirting, creative, support\n\n"
    "Examples:\n"
    "hey how are you?\njust checking in\n"
    "---\ngreeting chat\nsocial\n\n"
    "help me debug this traceback\nTypeError: NoneType is not subscriptable\n"
    "---\npython debugging\nwork\n\n"
    "can you explain how photosynthesis works?\n"
    "---\nphotosynthesis biology\ninformative\n\n"
    "hey choom you back?\nwe upgraded your optics\nlets play with the video settings\n"
    "---\nvideo generation\ncreative"
)


def _strip_prefix(line: str) -> str:
    """Remove label prefixes like 'Topic: ', 'Mode: ', 'Line 1: ' that the model sometimes adds."""
    return re.sub(r"^[a-zA-Z]+(?:\s+\d+)?\s*:\s*", "", line).strip()


def fetch_messages(conv_id: str) -> list:
    """Fetch messages for a conversation from AgentEgo API."""
    try:
        resp = requests.get(f"{EGO_URL}/api/sessions/{conv_id}/messages", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("Could not fetch messages for %s: %s", conv_id[:24], e)
        return []


def _make_excerpt(messages: list, max_chars: int = 600) -> str:
    """Build a compact excerpt from user messages for classification.

    User messages carry the intent (topic + mode). We skip assistant messages
    to avoid picking up filler phrases as the topic.
    """
    lines = []
    total = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        snippet = content[:250]
        if total + len(snippet) > max_chars:
            break
        lines.append(snippet)
        total += len(snippet)
        if len(lines) >= 6:
            break
    return "\n".join(lines)


def label_conversation(messages: list, title: str = "") -> tuple[str | None, str | None]:
    excerpt = _make_excerpt(messages)
    if not excerpt:
        excerpt = title or "untitled"
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "stream": False,
                "options": {"num_gpu": 0, "temperature": 0},
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": excerpt},
                ],
            },
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]

        # Topic: first line, strip any "Topic: " prefix, clean words, cap at 3
        topic = None
        if lines:
            words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in _strip_prefix(lines[0]).split()]
            words = [w for w in words if w][:3]
            topic = " ".join(words) or None

        # Mode: second line, strip any "Mode: " prefix, mapped to a known mode
        mode = None
        if len(lines) > 1:
            candidate = re.sub(r"[^a-z]", "", _strip_prefix(lines[1]).lower())
            mode = _canon_mode(candidate)
        # Fallback: scan entire raw output for any recognizable mode word
        if mode is None:
            for word in re.findall(r"[a-z]+", raw.lower()):
                m = _canon_mode(word)
                if m:
                    mode = m
                    break
        # Guarantee a valid mode when there's a topic, so a conversation can never
        # get stuck "unanalyzed" just because the model used an off-list word.
        if mode is None and topic:
            mode = "social"

        return topic, mode
    except Exception as e:
        log.warning("Ollama error: %s", e)
        return None, None


def check_trigger() -> bool:
    try:
        status = requests.get(f"{EGO_URL}/api/topic/status", timeout=5).json()
        if status.get("triggered"):
            requests.post(f"{EGO_URL}/api/topic/trigger-clear", timeout=5)
            return True
    except Exception:
        pass
    return False


def heartbeat():
    try:
        requests.post(f"{EGO_URL}/api/topic/heartbeat", timeout=5)
    except Exception:
        pass


def report_progress(current: int, total: int, session_id: str = ""):
    try:
        requests.post(
            f"{EGO_URL}/api/topic/progress",
            params={"current": current, "total": total, "session_id": session_id},
            timeout=5,
        )
    except Exception:
        pass


def process_pending():
    heartbeat()

    try:
        resp = requests.get(f"{EGO_URL}/api/topic/pending", timeout=10)
        resp.raise_for_status()
        pending = resp.json()
    except Exception as e:
        log.warning("Could not reach AgentEgo: %s", e)
        return

    if not pending:
        return

    log.info("%d conversation(s) pending analysis", len(pending))
    total = len(pending)

    for idx, item in enumerate(pending, start=1):
        conv_id = item["session_id"]
        report_progress(idx, total, conv_id)

        messages = fetch_messages(conv_id)
        topic, mode = label_conversation(messages, title=item.get("title") or "")
        if not topic and not mode:
            log.info("No labels generated for %s, skipping", conv_id[:24])
            continue

        try:
            requests.post(
                f"{EGO_URL}/api/topic/score",
                json={"session_id": conv_id, "topic": topic, "mode": mode},
                timeout=10,
            )
            log.info("Labeled %s → topic: %s | mode: %s", conv_id[:24], topic, mode)
        except Exception as e:
            log.warning("Failed to post labels for %s: %s", conv_id, e)

    report_progress(0, 0)
    try:
        requests.post(f"{EGO_URL}/api/topic/complete", timeout=5)
    except Exception:
        pass


def run():
    while True:
        process_pending()
        for _ in range(POLL_INTERVAL // 5):
            time.sleep(5)
            if check_trigger():
                log.info("Triggered by UI — running now")
                break


if __name__ == "__main__":
    log.info("Worker started. Model: %s (CPU-only). Polling %s every %ds", MODEL, EGO_URL, POLL_INTERVAL)
    run()
