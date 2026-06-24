import json
import time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from ..db.ego import get_ego_db
from ..db.hermes import get_session_messages

router = APIRouter(prefix="/api")


class SentimentScore(BaseModel):
    dominant: str
    top3: list[str]
    scores: dict[str, float]
    message_count: int


class SentimentResult(BaseModel):
    session_id: str
    user: Optional[SentimentScore] = None
    agent: Optional[SentimentScore] = None


@router.get("/sentiment/pending")
async def get_pending_sessions() -> list[str]:
    """Return session_ids that have agent:end events but no sentiment score yet."""
    conn = await get_ego_db()
    try:
        # Sessions that received agent:end
        cursor = await conn.execute(
            """
            SELECT DISTINCT session_id FROM events
            WHERE event_type = 'agent:end' AND session_id IS NOT NULL
            """
        )
        all_ended = {row[0] for row in await cursor.fetchall()}

        # Sessions already scored
        cursor = await conn.execute(
            "SELECT key FROM module_data WHERE module = 'sentiment'"
        )
        already_scored = {row[0] for row in await cursor.fetchall()}

        return list(all_ended - already_scored)
    finally:
        await conn.close()


@router.post("/sentiment/score", status_code=202)
async def save_sentiment_score(result: SentimentResult):
    """Store sentiment scores for a session."""
    conn = await get_ego_db()
    try:
        value = json.dumps({
            "user": result.user.model_dump() if result.user else None,
            "agent": result.agent.model_dump() if result.agent else None,
        })
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('sentiment', ?, ?, ?)
            ON CONFLICT(module, key) DO UPDATE SET value = excluded.value,
                                                   updated_at = excluded.updated_at
            """,
            (result.session_id, value, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "saved"}


@router.get("/sessions/{session_id}/messages")
async def get_messages_json(session_id: str) -> list[dict]:
    """Lightweight JSON endpoint for the sentiment worker."""
    rows = await get_session_messages(session_id)
    return [{"role": r["role"], "content": r["content"]} for r in rows
            if r.get("role") in ("user", "assistant") and r.get("content")]


@router.post("/sentiment/trigger", status_code=202)
async def trigger_scoring():
    """Set a flag so the sentiment worker runs immediately on next poll."""
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'sentiment_trigger', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "queued"}


@router.post("/sentiment/heartbeat", status_code=202)
async def worker_heartbeat():
    """Called by the worker each poll cycle to signal it's alive."""
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'sentiment_heartbeat', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "ok"}


@router.post("/sentiment/progress", status_code=202)
async def update_progress(current: int, total: int, session_id: str = ""):
    """Called by the worker as it scores each session."""
    conn = await get_ego_db()
    try:
        value = json.dumps({"current": current, "total": total, "session_id": session_id})
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'sentiment_progress', ?, ?)
            ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (value, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "ok"}


@router.post("/sentiment/trigger-clear", status_code=202)
async def clear_trigger():
    """Called by the worker after it picks up the trigger."""
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE module_data SET value='0' WHERE module='_system' AND key='sentiment_trigger'"
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "cleared"}


@router.get("/sentiment/status")
async def scoring_status() -> dict:
    """Return pending count, trigger state, worker health, and active progress."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE event_type='agent:end'"
        )
        total_ended = (await cursor.fetchone())[0] or 0

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM module_data WHERE module='sentiment'"
        )
        total_scored = (await cursor.fetchone())[0] or 0

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='sentiment_trigger'"
        )
        row = await cursor.fetchone()
        triggered = row is not None and row[0] == "1"

        cursor = await conn.execute(
            "SELECT updated_at FROM module_data WHERE module='sentiment' ORDER BY updated_at DESC LIMIT 1"
        )
        last_row = await cursor.fetchone()
        last_run = last_row[0] if last_row else None

        # Worker considered online if heartbeat within last 90s
        cursor = await conn.execute(
            "SELECT updated_at FROM module_data WHERE module='_system' AND key='sentiment_heartbeat'"
        )
        hb_row = await cursor.fetchone()
        worker_online = hb_row is not None and (time.time() - hb_row[0]) < 90

        # Active scoring progress
        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='sentiment_progress'"
        )
        prog_row = await cursor.fetchone()
        progress = None
        if prog_row and (time.time() - prog_row[1]) < 30:
            try:
                progress = json.loads(prog_row[0])
            except Exception:
                pass
    finally:
        await conn.close()

    return {
        "pending": max(0, total_ended - total_scored),
        "triggered": triggered,
        "last_run": last_run,
        "worker_online": worker_online,
        "progress": progress,
    }


@router.get("/sentiment/{session_id}")
async def get_session_sentiment(session_id: str) -> dict:
    """Return stored sentiment data for a session."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module = 'sentiment' AND key = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        return json.loads(row[0])
    finally:
        await conn.close()
