import time
import aiosqlite
from ..config import settings


def cutoff_ts() -> float:
    return time.time() - (settings.retention_days * 86400)


def _hermes_uri(db_path: str | None = None) -> str:
    return f"file:{db_path or settings.hermes_db_path}?mode=ro"


def _is_system_msg(row: dict) -> bool:
    """Filter out Hermes-injected system notifications (background process completions etc.)."""
    content = row.get("content") or ""
    return row.get("role") == "user" and content.startswith("[IMPORTANT:")


async def get_recent_sessions(db_path: str | None = None) -> list:
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
