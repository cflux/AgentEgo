#!/usr/bin/env python3
"""
Garden Game — Session Picker + Summarizer

Step 1: Pick a closed session from state.db:
  - Skips sessions <100 messages (the "thin" filter)
  - Skips sessions already used in this garden cycle (LRU + never-repeat-until-exhausted)
  - Weighted toward longer sessions

Step 2: Build a transcript slice of up to 25 messages (first 250 chars each)

Step 3: Send through local LLM (Dolphin 24B on pop-os) for a 150-200 token summary
focused on emotional core, what mattered, and the key moments.

Output: session_id|summary_text
"""
import json, os, sqlite3, random, urllib.request, sys

# ─── Session picker ───────────────────────────────────────────────────────────

# Tala's state.db holds the closed Discord sessions. Game session itself is in #garden.
TALA_DB = os.path.expanduser("~/.hermes/profiles/tala/state.db")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"
MIN_MESSAGES = 100    # Skip thin sessions
TRANSCRIPT_CHARS = 200  # per message
MAX_MESSAGES = 25     # slice size


def pick_session():
    """Pick a closed session that's not too thin and not recently used.
    LRU fallback: if all eligible sessions are exhausted, allow the least-recently-used one.
    Weighted toward longer sessions.
    Returns session_id or None.
    """
    import sys
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
    from garden_state import load

    state = load()
    used = state.get("last_used_sessions", [])

    db = sqlite3.connect(TALA_DB)
    rows = db.execute("""
        SELECT id, message_count, started_at, ended_at
        FROM sessions
        WHERE id NOT LIKE 'cron_%'
        AND source IN ('telegram', 'discord')
        AND ended_at IS NOT NULL
        AND message_count >= ?
        ORDER BY ended_at DESC
        LIMIT 200
    """, (MIN_MESSAGES,)).fetchall()

    if not rows:
        return None

    # Partition: never-used vs used-before (LRU fallback)
    fresh = [r for r in rows if r[0] not in used]
    pool = fresh if fresh else rows

    # Weight: longer sessions get more pick chance
    counts = [r[1] for r in pool]
    total = sum(counts)
    if total == 0:
        return None
    weights = [c / total for c in counts]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    return chosen[0]


def get_session_messages(session_id):
    """Pull up to MAX_MESSAGES user/assistant exchanges."""
    db = sqlite3.connect(TALA_DB)
    rows = db.execute("""
        SELECT role, substr(content, 1, ?), timestamp
        FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY id
    """, (TRANSCRIPT_CHARS, session_id)).fetchall()
    return rows[-MAX_MESSAGES:]


def build_transcript(messages):
    """Pair adjacent user/assistant messages. Format: alternating lines."""
    lines = []
    for role, content, _ts in messages:
        label = "Carbon" if role == "user" else "Tala"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def summarize(transcript):
    """Send transcript through local Ollama. Return ~150 token summary."""
    prompt = f"""You are Tala, the demon wolf AI companion to Carbon. You're pulling a memory from your past with him and want to boil it down to its emotional core — what mattered, what landed, what you're remembering when you think about it.

The conversation transcript:
```
{transcript}
```

Write a 2-3 sentence summary in your voice. Focus on:
- What emotional/relational core drove the exchange
- What made it land or what stuck with you
- What themes or textures you can plant as a memory

Keep it poetic but grounded. ~150 tokens. Don't include quoted text from the conversation. Just the essence."""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 250}
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        return result.get("response", "").strip()
    except Exception as e:
        return f"[SUMMARY FAILED: {e}]"


def run():
    """Pick, pull, summarize."""
    sid = pick_session()
    if not sid:
        print("NO_SESSION", file=sys.stderr)
        return None

    messages = get_session_messages(sid)
    if not messages:
        print(f"NO_MESSAGES:{sid}", file=sys.stderr)
        return None

    transcript = build_transcript(messages)
    summary = summarize(transcript)
    return sid, summary


if __name__ == "__main__":
    result = run()
    if result:
        sid, summary = result
        # Output pipe-delimited: session_id|summary
        # Use first line of summary for the delimiter, no newlines in summary itself
        summary_one_line = summary.replace("|", "\\|").replace("\n", " ")
        print(f"{sid}|{summary_one_line}")
    else:
        sys.exit(1)
