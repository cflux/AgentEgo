import json
import time
from fastapi import APIRouter
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
