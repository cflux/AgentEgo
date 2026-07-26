"""Ego-local message source for idoru agents (DESIGN §6b, idoru side).

idoru agents have no Hermes ``state.db``; instead idoru *pushes* their transcript into ego.db's
``ext_sessions``/``ext_messages`` tables via ``/api/ingest``. This module is the read side — it
mirrors the public accessor surface of ``db/hermes.py`` (same dict shapes) so the existing
conversation/round/scoring pipeline works unchanged — plus the write side used by the ingest router.

A profile is routed here (rather than to Hermes) when its ``db_path`` is the sentinel
``idoru://<profile>``. The Hermes code path is never touched.
"""
from __future__ import annotations

import time

from .ego import get_ego_db
from .hermes import cutoff_ts

IDORU_SCHEME = "idoru://"


def is_idoru_dbpath(db_path: str | None) -> bool:
    return bool(db_path) and db_path.startswith(IDORU_SCHEME)


def dbpath_for(profile: str) -> str:
    return f"{IDORU_SCHEME}{profile}"


def profile_from_dbpath(db_path: str) -> str:
    return db_path[len(IDORU_SCHEME):]


# --- read side (mirrors db/hermes.py dict shapes) ---

def _session_dict(row: dict) -> dict:
    """Shape an ext_sessions row like a Hermes session row. `source` carries the platform so
    conversations._platform_of() picks the right conversation gap."""
    return {
        "id": row["id"], "source": row["platform"], "user_id": row["user_id"],
        "model": None, "started_at": row["started_at"], "ended_at": row["ended_at"],
        "message_count": row["message_count"], "title": row["title"],
        "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
        "end_reason": None, "cwd": None,
    }


async def get_recent_sessions(profile: str) -> list:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT id, platform, user_id, title, started_at, ended_at, message_count "
            "FROM ext_sessions WHERE profile_name = ? AND started_at >= ? ORDER BY started_at DESC",
            (profile, cutoff_ts()),
        )
        return [_session_dict(dict(r)) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_recent_sessions_by_activity(profile: str) -> list:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            """
            SELECT s.id, s.platform, s.user_id, s.title, s.started_at, s.ended_at, s.message_count,
                   COALESCE((SELECT MAX(m.timestamp) FROM ext_messages m
                             WHERE m.profile_name = s.profile_name AND m.session_id = s.id
                               AND m.active = 1), s.started_at) AS last_activity
            FROM ext_sessions s
            WHERE s.profile_name = ? AND s.started_at >= ?
            ORDER BY last_activity DESC
            """,
            (profile, cutoff_ts()),
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            sd = _session_dict(d)
            sd["last_activity"] = d["last_activity"]
            out.append(sd)
        return out
    finally:
        await conn.close()


async def get_session(session_id: str, profile: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT id, platform, user_id, title, started_at, ended_at, message_count "
            "FROM ext_sessions WHERE profile_name = ? AND id = ?",
            (profile, session_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = _session_dict(dict(row))
        d.update({"model_config": None, "cache_read_tokens": 0, "cache_write_tokens": 0,
                  "reasoning_tokens": 0, "actual_cost_usd": 0.0, "api_call_count": 0,
                  "tool_call_count": 0})
        return d
    finally:
        await conn.close()


def _msg_dict(row: dict) -> dict:
    return {
        "id": row["id"], "role": row["role"], "content": row["content"],
        "tool_name": None, "tool_calls": None, "timestamp": row["timestamp"],
        "token_count": 0, "finish_reason": None, "reasoning_content": None,
        "active": row["active"], "compacted": 0,
    }


async def get_session_messages(session_id: str, profile: str) -> list:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT id, role, content, timestamp, active FROM ext_messages "
            "WHERE profile_name = ? AND session_id = ? AND active = 1 ORDER BY timestamp ASC",
            (profile, session_id),
        )
        return [_msg_dict(dict(r)) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_session_messages_in_range(session_id: str, start_ts: float, end_ts: float,
                                        profile: str) -> list:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT id, role, content, timestamp, active FROM ext_messages "
            "WHERE profile_name = ? AND session_id = ? AND active = 1 "
            "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
            (profile, session_id, start_ts, end_ts),
        )
        return [_msg_dict(dict(r)) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_session_stats(profile: str) -> dict:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            """
            SELECT COUNT(*) AS total_sessions, COALESCE(SUM(message_count),0) AS total_messages,
                   0 AS total_input_tokens, 0 AS total_output_tokens, 0.0 AS total_cost
            FROM ext_sessions WHERE profile_name = ? AND started_at >= ?
            """,
            (profile, cutoff_ts()),
        )
        row = await cur.fetchone()
        return dict(row) if row else {}
    finally:
        await conn.close()


# --- registry ---

async def register_agent(name: str, display_name: str | None = None, meta: str | None = None) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO agents (name, source, display_name, created_at, meta)
            VALUES (?, 'idoru', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, agents.display_name),
                meta = COALESCE(excluded.meta, agents.meta)
            """,
            (name, display_name, time.time(), meta),
        )
        await conn.commit()
    finally:
        await conn.close()


# --- write side (ingest) ---

async def ingest_messages(profile: str, session: dict, messages: list[dict]) -> int:
    """Upsert a pushed session + its messages, then recompute the session's bounds/count from the
    stored messages. Idempotent per (profile, message id). Returns the number of new messages."""
    await register_agent(profile)  # auto-register on first push
    sid = session["id"]
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO ext_sessions (id, profile_name, platform, user_id, title,
                                      started_at, ended_at, message_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(profile_name, id) DO UPDATE SET
                platform = COALESCE(excluded.platform, ext_sessions.platform),
                user_id  = COALESCE(excluded.user_id, ext_sessions.user_id),
                title    = COALESCE(excluded.title, ext_sessions.title)
            """,
            (sid, profile, session.get("platform"), session.get("user_id"),
             session.get("title"), session.get("started_at"), session.get("ended_at")),
        )
        new = 0
        for m in messages:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO ext_messages (id, profile_name, session_id, role, content, "
                "timestamp, active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (m["id"], profile, sid, m["role"], m.get("content") or "", m["timestamp"]),
            )
            new += cur.rowcount or 0
        # Recompute bounds + count from the actual stored messages.
        await conn.execute(
            """
            UPDATE ext_sessions SET
                started_at = COALESCE((SELECT MIN(timestamp) FROM ext_messages
                                       WHERE profile_name = ? AND session_id = ?), started_at),
                ended_at   = COALESCE((SELECT MAX(timestamp) FROM ext_messages
                                       WHERE profile_name = ? AND session_id = ?), ended_at),
                message_count = (SELECT COUNT(*) FROM ext_messages
                                 WHERE profile_name = ? AND session_id = ?)
            WHERE profile_name = ? AND id = ?
            """,
            (profile, sid, profile, sid, profile, sid, profile, sid),
        )
        await conn.commit()
        return new
    finally:
        await conn.close()
