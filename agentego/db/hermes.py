import time
import aiosqlite
from ..config import settings


def cutoff_ts() -> float:
    return time.time() - (settings.retention_days * 86400)


def _hermes_uri() -> str:
    return f"file:{settings.hermes_db_path}?mode=ro"


async def get_recent_sessions() -> list:
    async with aiosqlite.connect(_hermes_uri(), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            """
            SELECT id, source, user_id, model, started_at, ended_at,
                   message_count, title, input_tokens, output_tokens,
                   estimated_cost_usd, end_reason
            FROM sessions
            WHERE started_at >= ?
            ORDER BY started_at DESC
            """,
            (cutoff_ts(),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_session(session_id: str) -> dict | None:
    async with aiosqlite.connect(_hermes_uri(), uri=True) as conn:
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


async def get_session_messages(session_id: str) -> list:
    async with aiosqlite.connect(_hermes_uri(), uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only = true")
        await conn.execute("PRAGMA busy_timeout = 3000")
        cursor = await conn.execute(
            """
            SELECT id, role, content, tool_name, tool_calls, timestamp,
                   token_count, finish_reason, reasoning_content, active, compacted
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY timestamp ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_session_stats() -> dict:
    async with aiosqlite.connect(_hermes_uri(), uri=True) as conn:
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
