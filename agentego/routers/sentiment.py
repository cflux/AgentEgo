import json
import time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from ..db.ego import get_ego_db
from ..db.hermes import get_session_messages_in_range
from ..services.profiles import resolve_profile
from ..services.conversations import get_conversation, get_all_recent_conversations
from ..services import settings_store

router = APIRouter(prefix="/api")


async def _get_round(round_id: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT conversation_id, start_ts, end_ts FROM rounds WHERE id = ?", (round_id,)
        )
        row = await cursor.fetchone()
        return {"conversation_id": row[0], "start_ts": row[1], "end_ts": row[2]} if row else None
    finally:
        await conn.close()


class SentimentScore(BaseModel):
    dominant: str
    top3: list[str]
    scores: dict[str, float]
    message_count: int


class SentimentResult(BaseModel):
    session_id: str
    user: Optional[SentimentScore] = None
    agent: Optional[SentimentScore] = None
    # Optional per-round LLM mood predictions {mood_id: 0-10}; only set for round ids.
    mood_scores: Optional[dict] = None


@router.get("/sentiment/scoring-config")
async def scoring_config() -> dict:
    """Drives the sentiment worker: which backend, emotion taxonomy, mood catalog, LLM endpoint."""
    backend = await settings_store.get_setting("scoring_backend", "llm")
    taxonomy = await settings_store.get_emotion_taxonomy()
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT id, name, description FROM moods ORDER BY name")
        moods = [{"id": r[0], "name": r[1], "description": r[2]} for r in await cursor.fetchall()]
    finally:
        await conn.close()
    return {
        "backend": backend,
        "taxonomy": taxonomy,
        "moods": moods,
        "llm_url": await settings_store.get_setting("sentiment_llm_url", "http://localhost:11434"),
        "llm_model": await settings_store.get_setting("sentiment_llm_model", ""),
    }


async def _pending_sentiment_ids() -> list[str]:
    """Ids still needing a sentiment score (newest first). Under the LLM backend only rounds
    are scored directly — conversation sentiment is derived from rounds — so conversations are
    excluded. Under the GoEmotions backend both conversations and rounds are scored."""
    backend = await settings_store.get_setting("scoring_backend", "llm")
    conversations = [] if backend == "llm" else await get_all_recent_conversations()
    conn = await get_ego_db()
    try:
        cutoff = time.time() - 7 * 86400
        cursor = await conn.execute(
            "SELECT id FROM rounds WHERE end_ts >= ? ORDER BY end_ts DESC LIMIT 500", (cutoff,)
        )
        round_ids = [r[0] for r in await cursor.fetchall()]
        cursor = await conn.execute("SELECT key FROM module_data WHERE module='sentiment'")
        scored = {row[0] for row in await cursor.fetchall()}
    finally:
        await conn.close()
    pending = [c["id"] for c in conversations if c["id"] not in scored]
    pending += [rid for rid in round_ids if rid not in scored]
    return pending


@router.get("/sentiment/pending")
async def get_pending_sessions() -> list[str]:
    """Conversation and round UUIDs that have no sentiment score yet."""
    return await _pending_sentiment_ids()


async def _upsert_module(conn, module: str, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO module_data (module, key, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(module, key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
        """,
        (module, key, value, time.time()),
    )


async def _derive_conversation_sentiment(conn, round_id: str) -> None:
    """Recompute a conversation's sentiment by averaging its scored rounds' emotion scores,
    so the dashboard's conversation-level view stays populated without extra LLM calls."""
    cursor = await conn.execute("SELECT conversation_id FROM rounds WHERE id = ?", (round_id,))
    row = await cursor.fetchone()
    if not row:
        return
    conv_id = row[0]
    cursor = await conn.execute("SELECT id FROM rounds WHERE conversation_id = ?", (conv_id,))
    sibling_ids = [r[0] for r in await cursor.fetchall()]
    if not sibling_ids:
        return
    ph = ",".join("?" * len(sibling_ids))
    cursor = await conn.execute(
        f"SELECT value FROM module_data WHERE module='sentiment' AND key IN ({ph})", sibling_ids
    )
    rows = [r[0] for r in await cursor.fetchall()]

    def _agg(party: str) -> dict | None:
        totals: dict[str, float] = {}
        msgs = n = 0
        for raw in rows:
            try:
                p = (json.loads(raw) or {}).get(party)
            except Exception:
                p = None
            if not p:
                continue
            for k, v in (p.get("scores") or {}).items():
                totals[k] = totals.get(k, 0.0) + float(v)
            msgs += int(p.get("message_count") or 0)
            n += 1
        if not n:
            return None
        avg = {k: round(v / n, 4) for k, v in totals.items()}
        ranked = sorted(avg, key=avg.get, reverse=True)
        return {"dominant": ranked[0] if ranked else None, "top3": ranked[:3],
                "scores": avg, "message_count": msgs}

    derived = json.dumps({"user": _agg("user"), "agent": _agg("agent")})
    await _upsert_module(conn, "sentiment", conv_id, derived)


@router.post("/sentiment/score", status_code=202)
async def save_sentiment_score(result: SentimentResult):
    """Store sentiment scores for a session/round (+ optional LLM mood scores for rounds)."""
    conn = await get_ego_db()
    try:
        value = json.dumps({
            "user": result.user.model_dump() if result.user else None,
            "agent": result.agent.model_dump() if result.agent else None,
        })
        await _upsert_module(conn, "sentiment", result.session_id, value)
        if result.mood_scores is not None:
            await _upsert_module(conn, "mood_scores", result.session_id, json.dumps(result.mood_scores))
        # If this id is a round, refresh its parent conversation's derived sentiment.
        await _derive_conversation_sentiment(conn, result.session_id)
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "saved"}


@router.get("/sessions/{conv_or_session_id}/messages")
async def get_messages_json(conv_or_session_id: str, profile: str = "") -> list[dict]:
    """Lightweight JSON endpoint for the sentiment worker. Accepts conv UUID or legacy session_id."""
    conv = await get_conversation(conv_or_session_id)
    if conv:
        db_path = resolve_profile(conv["profile_name"])
        rows = await get_session_messages_in_range(
            conv["session_id"], conv["start_ts"], conv["end_ts"], db_path=db_path
        )
    elif (rnd := await _get_round(conv_or_session_id)):
        parent = await get_conversation(rnd["conversation_id"])
        if parent:
            db_path = resolve_profile(parent["profile_name"])
            rows = await get_session_messages_in_range(
                parent["session_id"], rnd["start_ts"], rnd["end_ts"], db_path=db_path
            )
        else:
            rows = []
    else:
        # Legacy fallback: treat as raw session_id
        from ..db.hermes import find_session_messages
        db_path = resolve_profile(profile) if profile else None
        if db_path:
            from ..db.hermes import get_session_messages
            rows = await get_session_messages(conv_or_session_id, db_path=db_path)
        else:
            rows = await find_session_messages(conv_or_session_id)
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
    return JSONResponse({"status": "queued"}, headers={"HX-Trigger": "sentimentUpdate"})


@router.post("/sentiment/rescore", status_code=202)
async def rescore_all():
    """Full re-score: clear existing round emotion + mood scores (making them pending) and trigger
    the worker. Use after adding/changing moods so the LLM re-scores rounds with the new catalog.
    Conversation sentiment is re-derived from rounds automatically as they're re-scored."""
    conn = await get_ego_db()
    try:
        cutoff = time.time() - 7 * 86400
        cursor = await conn.execute("SELECT id FROM rounds WHERE end_ts >= ?", (cutoff,))
        round_ids = [r[0] for r in await cursor.fetchall()]
        if round_ids:
            ph = ",".join("?" * len(round_ids))
            await conn.execute(
                f"DELETE FROM module_data WHERE module='sentiment' AND key IN ({ph})", round_ids
            )
        await conn.execute("DELETE FROM module_data WHERE module='mood_scores'")
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
    return JSONResponse({"status": "queued", "rounds": len(round_ids)},
                        headers={"HX-Trigger": "sentimentUpdate"})


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


@router.post("/sentiment/complete", status_code=202)
async def scoring_complete():
    """Called by the worker when a scoring run finishes."""
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'sentiment_complete', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
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
    pending_count = len(await _pending_sentiment_ids())

    conn = await get_ego_db()
    try:
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
                if progress.get("total", 0) == 0:
                    progress = None
            except Exception:
                pass

        # One-shot "just finished" flag — read and clear atomically
        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='sentiment_complete'"
        )
        complete_row = await cursor.fetchone()
        just_completed = (
            complete_row is not None
            and complete_row[0] == "1"
            and (time.time() - complete_row[1]) < 10
        )
        if just_completed:
            await conn.execute(
                "UPDATE module_data SET value='0' WHERE module='_system' AND key='sentiment_complete'"
            )
            await conn.commit()
    finally:
        await conn.close()

    return {
        "pending": pending_count,
        "triggered": triggered,
        "last_run": last_run,
        "worker_online": worker_online,
        "progress": progress,
        "just_completed": just_completed,
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
