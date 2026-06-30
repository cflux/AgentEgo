import json
import time
from uuid import uuid4
from ..db.ego import get_ego_db
from ..db.hermes import get_recent_sessions, get_session_messages
from .settings_store import get_setting

CONV_GAP_SECONDS = 7200  # default: 2-hour gap = new conversation
CONV_SETTLE_SECONDS = 600  # don't re-score a conversation until it's been quiet this long

# Messaging platforms hold continuous chats with only short natural pauses, so they
# split on a much shorter gap than long-thinking CLI/coding sessions.
_CHAT_PLATFORMS = {"telegram", "discord", "signal", "whatsapp", "slack", "messenger"}


def _platform_of(source) -> str:
    """Extract a platform name from a session 'source' (JSON object or plain string)."""
    if not source:
        return ""
    try:
        data = json.loads(source)
        if isinstance(data, dict):
            return (data.get("platform") or "").lower()
    except (json.JSONDecodeError, TypeError):
        pass
    return str(source).strip().lower()


async def _gap_for_source(source) -> float:
    """Resolve the split-gap (seconds) for a session, by platform, from settings."""
    chat = _platform_of(source) in _CHAT_PLATFORMS
    key = "conv_gap_chat_minutes" if chat else "conv_gap_minutes"
    default = "30" if chat else "120"
    try:
        return float(await get_setting(key, default)) * 60.0
    except (TypeError, ValueError):
        return float(default) * 60.0


def split_messages(messages: list, gap_seconds: float = CONV_GAP_SECONDS) -> list:
    """Split a sorted message list into conversation segments on gaps >= gap_seconds."""
    if not messages:
        return []
    segments: list[list] = []
    current = [messages[0]]
    for msg in messages[1:]:
        gap = (msg.get("timestamp") or 0) - (current[-1].get("timestamp") or 0)
        if gap >= gap_seconds:
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
            "messages": seg,
            "title": next(
                (m["content"][:120] for m in seg
                 if m.get("role") == "user" and m.get("content")),
                None,
            ),
        }
        for i, seg in enumerate(segments)
    ]


ROUND_BUILD_WINDOW = 2 * 86400  # only maintain rounds for conversations active within ~2 days


async def _round_exchanges() -> int:
    try:
        return max(1, int(await get_setting("round_exchanges", "3")))
    except (TypeError, ValueError):
        return 3


def split_into_rounds(messages: list, exchanges_per_round: int = 3) -> list:
    """Group a conversation's messages into 'rounds' of ~N exchanges. An exchange is a
    contiguous run of user message(s) followed by a contiguous run of agent message(s);
    a new exchange begins when the user speaks again after the agent replied."""
    if not messages:
        return []
    exchanges: list[list] = []
    cur: list = []
    seen_agent = False
    for m in messages:
        if m.get("role") == "user" and seen_agent and cur:
            exchanges.append(cur)
            cur = []
            seen_agent = False
        cur.append(m)
        if m.get("role") == "assistant":
            seen_agent = True
    if cur:
        exchanges.append(cur)

    rounds = []
    for i in range(0, len(exchanges), exchanges_per_round):
        bundle = [m for ex in exchanges[i:i + exchanges_per_round] for m in ex]
        rounds.append({
            "round_index": len(rounds),
            "start_ts": bundle[0].get("timestamp") or 0.0,
            "end_ts": bundle[-1].get("timestamp") or 0.0,
            "msg_count": len(bundle),
        })
    return rounds


async def _sync_rounds(conn, conversation_id: str, profile_name: str,
                       messages: list, exchanges_per_round: int, now: float) -> None:
    """Build/reconcile a conversation's rounds, mirroring the conversation reconcile:
    preserve round ids (so round sentiment stays valid), settle-aware re-enrichment."""
    rounds = split_into_rounds(messages, exchanges_per_round)
    cursor = await conn.execute(
        "SELECT round_index, id, msg_count FROM rounds WHERE conversation_id = ?",
        (conversation_id,),
    )
    existing = {r[0]: (r[1], r[2]) for r in await cursor.fetchall()}
    total = len(rounds)
    stale: list[str] = []
    for rd in rounds:
        idx = rd["round_index"]
        if idx in existing:
            rid, old = existing[idx]
            await conn.execute(
                "UPDATE rounds SET start_ts = ?, end_ts = ?, msg_count = ? WHERE id = ?",
                (rd["start_ts"], rd["end_ts"], rd["msg_count"], rid),
            )
            if old != rd["msg_count"] and (now - (rd["end_ts"] or 0)) >= CONV_SETTLE_SECONDS:
                stale.append(rid)
        else:
            await conn.execute(
                "INSERT INTO rounds (id, conversation_id, profile_name, round_index, "
                "start_ts, end_ts, msg_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), conversation_id, profile_name, idx,
                 rd["start_ts"], rd["end_ts"], rd["msg_count"], now),
            )
    for idx, (rid, _) in existing.items():
        if idx >= total:
            await conn.execute("DELETE FROM rounds WHERE id = ?", (rid,))
            stale.append(rid)
    for rid in stale:
        await conn.execute("DELETE FROM module_data WHERE key = ? AND module = 'sentiment'", (rid,))


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
    gap = await _gap_for_source(session.get("source"))
    parts = split_messages(msgs, gap_seconds=gap)
    if not parts:
        started = session.get("started_at") or 0.0
        parts = [{
            "part_index": 0, "part_total": 1,
            "start_ts": started, "end_ts": started,
            "msg_count": 0, "title": session.get("title"),
        }]

    now = time.time()
    total = len(parts)
    exchanges_per_round = await _round_exchanges()
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT part_index, id, msg_count FROM conversations WHERE session_id = ? AND profile_name = ?",
            (session_id, profile_name),
        )
        existing = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
        stale_ids: list[str] = []  # conversations whose content changed → re-enrich

        for part in parts:
            idx = part["part_index"]
            if idx in existing:
                cid, old_count = existing[idx]
                await conn.execute(
                    "UPDATE conversations SET part_total = ?, start_ts = ?, end_ts = ?, "
                    "msg_count = ?, title = ? WHERE id = ?",
                    (total, part["start_ts"], part["end_ts"], part["msg_count"],
                     part["title"], cid),
                )
                # Re-enrich a grown part only once it has SETTLED — an actively
                # growing conversation keeps its current labels (no blank flicker)
                # until it's been quiet, then gets one re-score on complete content.
                if old_count != part["msg_count"] and (now - (part["end_ts"] or 0)) >= CONV_SETTLE_SECONDS:
                    stale_ids.append(cid)
            else:
                cid = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO conversations
                        (id, session_id, profile_name, part_index, part_total,
                         start_ts, end_ts, msg_count, title, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid, session_id, profile_name, idx, total,
                     part["start_ts"], part["end_ts"], part["msg_count"], part["title"], now),
                )
            # Maintain rounds for recent conversations only (mood reads recent rounds).
            if (now - (part["end_ts"] or 0)) < ROUND_BUILD_WINDOW:
                await _sync_rounds(conn, cid, profile_name, part.get("messages") or [],
                                   exchanges_per_round, now)

        # Re-splitting (e.g. a smaller gap) can leave orphaned higher-index parts.
        for idx, (cid, _) in existing.items():
            if idx >= total:
                rids = [r[0] for r in await (await conn.execute(
                    "SELECT id FROM rounds WHERE conversation_id = ?", (cid,))).fetchall()]
                for rid in rids:
                    await conn.execute("DELETE FROM module_data WHERE key = ? AND module = 'sentiment'", (rid,))
                await conn.execute("DELETE FROM rounds WHERE conversation_id = ?", (cid,))
                await conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
                stale_ids.append(cid)

        # Drop now-mismatched enrichment so the workers re-score the changed parts.
        for cid in stale_ids:
            await conn.execute(
                "DELETE FROM module_data WHERE key = ? AND module IN ('sentiment','topic','mode')",
                (cid,),
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


def sort_conversations_grouped(conversations: list) -> list:
    """Order conversations so a session's parts stay together and in order:
    most-recently-active session first, then Part 1, 2, 3… within it. Shared by the
    sessions page and the dashboard's recent box so the two always agree."""
    recency: dict = {}
    for c in conversations:
        key = (c.get("profile_name"), c.get("session_id"))
        recency[key] = max(recency.get(key, 0.0), c.get("end_ts") or 0.0)
    return sorted(conversations, key=lambda c: (
        -recency[(c.get("profile_name"), c.get("session_id"))],
        c.get("session_id") or "",
        c.get("part_index") or 0,
    ))


async def invalidate_stale_enrichment(margin: float = 180.0, settle: float = 600.0) -> int:
    """Clear sentiment/topic/mode for conversations whose content (end_ts) is newer
    than when they were scored — i.e. they grew after enrichment ran — so they get
    re-scored. Only touches *settled* conversations (no new messages for `settle`
    seconds); an actively-growing conversation keeps its current labels until it goes
    quiet, so its tags don't flicker between page loads."""
    now = time.time()
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT m.module, m.key FROM module_data m
            JOIN conversations c ON c.id = m.key
            WHERE m.module IN ('sentiment', 'topic', 'mode')
              AND c.end_ts > m.updated_at + ?
              AND c.end_ts < ?
            """,
            (margin, now - settle),
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
    """Sync recent Hermes sessions, re-syncing any whose message_count changed.

    Enrichment for a changed conversation is invalidated inside
    sync_session_conversations (once per actual content change, gated by the
    message_count watermark) — NOT on every read — so a settled conversation's
    tags stay put and all pages render the same DB state."""
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


async def get_recent_rounds(profile_name: str, limit: int = 20) -> list:
    """Fetch the most recent rounds (mood data points) for a profile, newest first."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, conversation_id, round_index, start_ts, end_ts, msg_count FROM rounds "
            "WHERE profile_name = ? ORDER BY end_ts DESC LIMIT ?",
            (profile_name, limit),
        )
        return [
            {"id": r[0], "conversation_id": r[1], "round_index": r[2],
             "start_ts": r[3], "end_ts": r[4], "msg_count": r[5]}
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
