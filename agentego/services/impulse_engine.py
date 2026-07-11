"""Impulse engine — shared helpers for the v2 impulse system.

The decision logic lives in `impulse_arbiter` (the generative arbiter). This module retains the two
cross-cutting helpers it depends on: the idle clock (last real user activity, excluding cron turns) and
the fire log (impulse_log) read/write.
"""
import time
from uuid import uuid4
from ..db.ego import get_ego_db


# --- Recency / idle ---

async def get_last_activity_ts(profile_name: str, db_path: str | None = None) -> float | None:
    """Most recent *user* conversation end_ts = 'last talked to user'. Excludes cron/sidequest
    sessions (e.g. impulse turns, session id 'cron_…') so a self-initiated action doesn't reset the
    idle clock — otherwise the outward idle-gate would never see genuine user absence."""
    from .conversations import sync_recent_conversations, get_recent_conversations
    try:
        await sync_recent_conversations(profile_name, db_path=db_path)
    except Exception:
        pass
    for c in await get_recent_conversations(profile_name, limit=25):
        if str(c.get("session_id") or "").startswith("cron_"):
            continue
        return c["end_ts"]
    return None


# --- Fire log ---

async def _log_fire(profile_name: str, action: dict, prompt: str, mood_id, idle_minutes: float) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO impulse_log (id, profile_name, action_id, label, prompt, mood_id, idle_minutes, fired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), profile_name, action["id"], action["label"], prompt, mood_id, idle_minutes, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def count_fires_since(profile_name: str, action_id: str, since_ts: float) -> int:
    """How many times a capability has fired since `since_ts` — for per-action daily budgets."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM impulse_log WHERE profile_name = ? AND action_id = ? AND fired_at >= ?",
            (profile_name, action_id, since_ts),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()


async def get_recent_log(profile_name: str, limit: int = 15) -> list[dict]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT label, prompt, mood_id, idle_minutes, fired_at FROM impulse_log "
            "WHERE profile_name = ? ORDER BY fired_at DESC LIMIT ?",
            (profile_name, limit),
        )
        return [
            {"label": r[0], "prompt": r[1], "mood_id": r[2], "idle_minutes": r[3], "fired_at": r[4]}
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()
