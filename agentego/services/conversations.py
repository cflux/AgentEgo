import time
from uuid import uuid4
from ..db.ego import get_ego_db
from ..db.hermes import get_recent_sessions, get_session_messages

CONV_GAP_SECONDS = 7200  # 2-hour gap = new conversation


def split_messages(messages: list) -> list:
    """Split a sorted message list into conversation segments on gaps >= CONV_GAP_SECONDS."""
    if not messages:
        return []
    segments: list[list] = []
    current = [messages[0]]
    for msg in messages[1:]:
        gap = (msg.get("timestamp") or 0) - (current[-1].get("timestamp") or 0)
        if gap >= CONV_GAP_SECONDS:
            segments.append(current)
            current = []
        current.append(msg)
    segments.append(current)
    total = len(segments)
    return [
        {
            "part_index": i,
            "part_total": total,
            "start_ts": seg[0].get("timestamp") or 0.0,
            "end_ts": seg[-1].get("timestamp") or 0.0,
            "msg_count": len(seg),
            "title": next(
                (m["content"][:120] for m in seg
                 if m.get("role") == "user" and m.get("content")),
                None,
            ),
        }
        for i, seg in enumerate(segments)
    ]


async def _get_sync_watermarks(profile_name: str) -> dict:
    """{session_id: last-synced Hermes message_count} for this profile."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT key, value FROM module_data WHERE module = '_conv_sync' AND key LIKE ?",
            (f"{profile_name}|%",),
        )
        out: dict = {}
        for key, value in await cursor.fetchall():
            sid = key.split("|", 1)[1]
            try:
                out[sid] = int(value)
            except (TypeError, ValueError):
                out[sid] = -1
        return out
    finally:
        await conn.close()


async def sync_session_conversations(
    session: dict, profile_name: str, db_path: str | None = None
) -> None:
    """Sync a Hermes session into ego.db conversations, idempotently.

    Re-splits the session's messages and reconciles by part_index: existing
    conversation rows are UPDATED in place (preserving their id, so sentiment/
    topic enrichment keyed on the conversation id stays valid) and any new
    segments (e.g. afternoon activity after a gap) are INSERTed. This makes
    long-running / re-activated sessions keep current instead of freezing at
    their first sync."""
    session_id = session["id"]

    msgs = await get_session_messages(session_id, db_path=db_path)
    parts = split_messages(msgs)
    if not parts:
        started = session.get("started_at") or 0.0
        parts = [{
            "part_index": 0, "part_total": 1,
            "start_ts": started, "end_ts": started,
            "msg_count": 0, "title": session.get("title"),
        }]

    now = time.time()
    total = len(parts)
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT part_index, id FROM conversations WHERE session_id = ? AND profile_name = ?",
            (session_id, profile_name),
        )
        existing = {row[0]: row[1] for row in await cursor.fetchall()}

        for part in parts:
            idx = part["part_index"]
            if idx in existing:
                await conn.execute(
                    "UPDATE conversations SET part_total = ?, start_ts = ?, end_ts = ?, "
                    "msg_count = ?, title = ? WHERE id = ?",
                    (total, part["start_ts"], part["end_ts"], part["msg_count"],
                     part["title"], existing[idx]),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO conversations
                        (id, session_id, profile_name, part_index, part_total,
                         start_ts, end_ts, msg_count, title, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), session_id, profile_name, idx, total,
                     part["start_ts"], part["end_ts"], part["msg_count"], part["title"], now),
                )

        # Record the watermark so we only re-sync when message_count changes.
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_conv_sync', ?, ?, ?)
            ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (f"{profile_name}|{session_id}", str(session.get("message_count") or 0), now),
        )
        await conn.commit()
    finally:
        await conn.close()


async def invalidate_stale_enrichment(margin: float = 180.0) -> int:
    """Clear sentiment/topic/mode for conversations whose content (end_ts) is newer
    than when they were scored — i.e. they grew after enrichment ran. This drops them
    back into the workers' "pending" queue so the labels refresh instead of going stale."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT m.module, m.key FROM module_data m
            JOIN conversations c ON c.id = m.key
            WHERE m.module IN ('sentiment', 'topic', 'mode')
              AND c.end_ts > m.updated_at + ?
            """,
            (margin,),
        )
        stale = await cursor.fetchall()
        for module, key in stale:
            await conn.execute("DELETE FROM module_data WHERE module = ? AND key = ?", (module, key))
        if stale:
            await conn.commit()
        return len(stale)
    finally:
        await conn.close()


async def sync_recent_conversations(profile_name: str, db_path: str | None = None) -> None:
    """Sync recent Hermes sessions, re-syncing any whose message_count changed,
    then invalidate any now-stale enrichment so it gets re-scored."""
    try:
        sessions = await get_recent_sessions(db_path=db_path)
    except Exception:
        return
    watermarks = await _get_sync_watermarks(profile_name)
    for s in sessions:
        mc = int(s.get("message_count") or 0)
        if watermarks.get(s["id"]) != mc:
            try:
                await sync_session_conversations(s, profile_name, db_path=db_path)
            except Exception:
                pass
    try:
        await invalidate_stale_enrichment()
    except Exception:
        pass


async def get_recent_conversations(profile_name: str, limit: int = 100) -> list:
    """Fetch conversations from ego.db ordered by end_ts DESC."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT id, session_id, profile_name, part_index, part_total,
                   start_ts, end_ts, msg_count, title
            FROM conversations
            WHERE profile_name = ?
            ORDER BY end_ts DESC
            LIMIT ?
            """,
            (profile_name, limit),
        )
        return [
            {
                "id": r[0], "session_id": r[1], "profile_name": r[2],
                "part_index": r[3], "part_total": r[4],
                "start_ts": r[5], "end_ts": r[6],
                "msg_count": r[7], "title": r[8],
            }
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()


async def get_all_recent_conversations() -> list:
    """Conversations from all profiles sorted by end_ts DESC."""
    from .profiles import discover_profiles
    profiles = discover_profiles()
    all_convs: list = []
    for p in profiles:
        try:
            convs = await get_recent_conversations(p["name"])
            all_convs.extend(convs)
        except Exception:
            pass
    all_convs.sort(key=lambda c: c.get("end_ts") or 0, reverse=True)
    return all_convs


async def get_conversation(conv_id: str) -> dict | None:
    """Fetch a single conversation row by UUID."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT id, session_id, profile_name, part_index, part_total,
                   start_ts, end_ts, msg_count, title
            FROM conversations WHERE id = ?
            """,
            (conv_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "session_id": row[1], "profile_name": row[2],
            "part_index": row[3], "part_total": row[4],
            "start_ts": row[5], "end_ts": row[6],
            "msg_count": row[7], "title": row[8],
        }
    finally:
        await conn.close()


async def get_first_conv_id_for_session(session_id: str) -> str | None:
    """Return the UUID of the first conversation (part 0) for a session_id."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id FROM conversations WHERE session_id = ? ORDER BY part_index ASC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()
