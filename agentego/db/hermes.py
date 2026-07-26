import time
import aiosqlite
from ..config import settings


def cutoff_ts() -> float:
    return time.time() - (settings.retention_days * 86400)


def _hermes_uri(db_path: str | None = None) -> str:
    return f"file:{db_path or settings.hermes_db_path}?mode=ro"


def _is_idoru(db_path: str | None) -> bool:
    """An idoru agent is addressed by the sentinel db_path ``idoru://<profile>`` (DESIGN §6b).
    When set, the reader delegates to the ego-local source instead of a Hermes state.db."""
    return bool(db_path) and db_path.startswith("idoru://")


def _is_system_msg(row: dict) -> bool:
    """Filter out Hermes-injected system notifications (background process completions etc.)."""
    content = row.get("content") or ""
    return row.get("role") == "user" and content.startswith("[IMPORTANT:")


async def get_recent_sessions(db_path: str | None = None) -> list:
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_recent_sessions(idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            """
            SELECT id, source, user_id, model, started_at, ended_at,
                   message_count, title, input_tokens, output_tokens,
                   estimated_cost_usd, end_reason, cwd
            FROM sessions
            WHERE started_at >= ?
            ORDER BY started_at DESC
            """,
            (cutoff_ts(),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_recent_sessions_by_activity(db_path: str | None = None) -> list:
    """Like get_recent_sessions, but ordered by last *message* time (newest activity first).

    A long-running session that's still receiving messages must outrank a shorter session that
    merely *started* more recently — otherwise callers that want "the current conversation" (mood
    exit judge, impulse arbiter) read the wrong transcript. ended_at is unreliable for this (it's
    NULL while a session is live), so activity is derived from MAX(messages.timestamp)."""
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_recent_sessions_by_activity(idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            """
            SELECT s.id, s.source, s.user_id, s.model, s.started_at, s.ended_at,
                   s.message_count, s.title, s.input_tokens, s.output_tokens,
                   s.estimated_cost_usd, s.end_reason, s.cwd,
                   COALESCE((SELECT MAX(m.timestamp) FROM messages m
                             WHERE m.session_id = s.id AND m.active = 1),
                            s.started_at) AS last_activity
            FROM sessions s
            WHERE s.started_at >= ?
            ORDER BY last_activity DESC
            """,
            (cutoff_ts(),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_recent_sessions() -> list:
    """Aggregate sessions from all discovered profiles, tagged with profile_name."""
    from ..services.profiles import discover_profiles
    profiles = discover_profiles()
    all_sessions = []
    for p in profiles:
        try:
            rows = await get_recent_sessions(db_path=p["db_path"])
            for r in rows:
                r["profile_name"] = p["name"]
            all_sessions.extend(rows)
        except Exception:
            pass
    all_sessions.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
    return all_sessions


async def get_session(session_id: str, db_path: str | None = None) -> dict | None:
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_session(session_id, idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            """
            SELECT id, source, user_id, model, model_config, started_at, ended_at,
                   message_count, title, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd, actual_cost_usd, api_call_count,
                   tool_call_count, end_reason, cwd
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def find_session(session_id: str) -> tuple[dict | None, str | None]:
    """Search all profiles for session_id. Returns (session_dict, profile_name)."""
    from ..services.profiles import discover_profiles
    for p in discover_profiles():
        row = await get_session(session_id, db_path=p["db_path"])
        if row:
            return row, p["name"]
    return None, None


_MSG_COLS = """
    SELECT id, role, content, tool_name, tool_calls, timestamp,
           token_count, finish_reason, reasoning_content, active, compacted
    FROM messages
"""


async def get_session_messages(session_id: str, db_path: str | None = None) -> list:
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_session_messages(session_id, idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            _MSG_COLS + "WHERE session_id = ? AND active = 1 ORDER BY timestamp ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [d for d in (dict(r) for r in rows) if not _is_system_msg(d)]


async def get_session_messages_in_range(
    session_id: str, start_ts: float, end_ts: float, db_path: str | None = None
) -> list:
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_session_messages_in_range(
            session_id, start_ts, end_ts, idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            _MSG_COLS + "WHERE session_id = ? AND active = 1 AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
            (session_id, start_ts, end_ts),
        )
        rows = await cursor.fetchall()
        return [d for d in (dict(r) for r in rows) if not _is_system_msg(d)]


async def find_session_messages(session_id: str) -> list:
    """Search all profiles for messages belonging to session_id."""
    from ..services.profiles import discover_profiles
    for p in discover_profiles():
        rows = await get_session_messages(session_id, db_path=p["db_path"])
        if rows:
            return rows
    return []


async def get_sessions_by_ids(session_ids: list[str], db_path: str | None = None) -> list:
    if not session_ids:
        return []
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        ph = ",".join("?" * len(session_ids))
        cursor = await conn.execute(
            f"""
            SELECT id, source, user_id, model, started_at, ended_at,
                   message_count, title, input_tokens, output_tokens,
                   estimated_cost_usd, end_reason, cwd
            FROM sessions WHERE id IN ({ph})
            """,
            session_ids,
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_session_stats(db_path: str | None = None) -> dict:
    if _is_idoru(db_path):
        from . import idoru_local
        return await idoru_local.get_session_stats(idoru_local.profile_from_dbpath(db_path))
    async with aiosqlite.connect(_hermes_uri(db_path), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cut = cutoff_ts()
        cursor = await conn.execute(
            """
            SELECT
                COUNT(*)              AS total_sessions,
                SUM(message_count)    AS total_messages,
                SUM(input_tokens)     AS total_input_tokens,
                SUM(output_tokens)    AS total_output_tokens,
                SUM(estimated_cost_usd) AS total_cost
            FROM sessions
            WHERE started_at >= ?
            """,
            (cut,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def get_all_session_stats() -> dict:
    """Aggregate stats across all discovered profiles."""
    from ..services.profiles import discover_profiles
    totals: dict = {}
    for p in discover_profiles():
        try:
            s = await get_session_stats(db_path=p["db_path"])
            for k, v in s.items():
                if v is not None:
                    totals[k] = (totals.get(k) or 0) + v
        except Exception:
            pass
    return totals
