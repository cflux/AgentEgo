#!/usr/bin/env python3
"""
Tala Conversation End Detector — Phase 2
Added: detailed logging, turn count gate (don't retire short sessions)
"""
import sqlite3, json, urllib.request, os, datetime

DB_PATH = os.path.expanduser("~/.hermes/profiles/tala/state.db")
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"
LOG = os.path.expanduser("~/.hermes/logs/tala_session_end.log")
MIN_TURNS = 10  # Don't retire sessions with fewer than this many exchanges


def get_recent_messages(db_path, limit=6):
    """Get current session, last N messages, and turn count."""
    # Get the current TALA session (not cron, only ACTIVE/not-ended)
    db = sqlite3.connect(db_path)
    session = db.execute(
        "SELECT id FROM sessions WHERE id NOT LIKE 'cron_%' AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not session:
        return None, None, 0
    session_id = session[0]
    
    rows = db.execute(
        """SELECT role, substr(content, 1, 200), timestamp
           FROM messages
           WHERE session_id = ? AND role IN ('user', 'assistant')
           ORDER BY id DESC
           LIMIT ?""",
        (session_id, limit)
    ).fetchall()
    rows.reverse()
    
    # Count total user+assistant messages (turns)
    count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role IN ('user', 'assistant')",
        (session_id,)
    ).fetchone()[0]
    
    return session_id, rows, count


def classify_conversation(messages):
    """Ask Dolphin 24B if this conversation is complete."""
    if not messages:
        return None
    
    transcript = ""
    for role, content, ts in messages[-6:]:
        speaker = "User" if role == "user" else "Tala"
        transcript += f"{speaker}: {content[:150]}\n"
    
    prompt = f"""You are a conversation classifier. Determine if this chat between a user and their AI companion has naturally concluded.

DONE signals: goodbyes, goodnight, natural wrap-up with no pending questions, simple acknowledgment after a conclusion, emotional closure.
ACTIVE signals: open questions, user said they'd return ("brb", "back later"), setup for more discussion, emotional tone suggesting continuation.

Transcript:
{transcript}

Reply with exactly ONE word: "done" or "active". Do not explain."""

    try:
        req = urllib.request.Request(
            OLLAMA,
            data=json.dumps({
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 5}
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        response = result.get("response", "").strip().lower()
        return "done" if "done" in response else "active" if "active" in response else f"unknown:{response[:20]}"
    except Exception as e:
        return f"error:{e}"


def log_detailed(session_id, verdict, turn_count, messages, gated=False):
    """Log full classification details for next-day verification."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last_msg = messages[-1][1][:80] if messages else "(none)"
    last_role = messages[-1][0] if messages else "?"
    last_time = datetime.datetime.fromtimestamp(messages[-1][2]).strftime("%H:%M") if messages else "?"
    
    gate_info = f"GATED({turn_count}<{MIN_TURNS})" if gated else ""
    action = "would restart" if verdict == "done" and not gated else "keep alive"
    
    with open(LOG, "a") as f:
        f.write(f"{now} | {session_id[:20]} | turns={turn_count:3d} | last={last_role}:{last_time} | "
                f"verdict={verdict:10s} {gate_info:20s} | {action}\n")
        if not gated and verdict == "done":
            f.write(f"  Last message: {last_msg}\n")


if __name__ == "__main__":
    session_id, messages, turn_count = get_recent_messages(DB_PATH)
    
    if not session_id:
        print("No active session")
    else:
        verdict = classify_conversation(messages)
        
        # Gate: don't retire sessions with fewer than MIN_TURNS exchanges
        gated = turn_count < MIN_TURNS and verdict == "done"
        effective_verdict = "active" if gated else verdict
        
        log_detailed(session_id, effective_verdict, turn_count, messages, gated=gated)
        
        print(f"Session: {session_id[:20]} | Turns: {turn_count} | Verdict: {verdict}")
        if gated:
            print(f"  ⚠️ GATED — session too short ({turn_count} < {MIN_TURNS} min turns)")
            print(f"  Keeping alive despite 'done' verdict")
        elif verdict == "done":
            print(f"  ✅ Session complete — gateway restart triggered")
        else:
            print(f"  🟢 Active — keeping alive")
