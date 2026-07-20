"""Impulse engine — shared helpers for the v2 impulse system.

The decision logic lives in `impulse_arbiter` (the generative arbiter). This module retains the two
cross-cutting helpers it depends on: the idle clock (last real user activity, excluding cron turns) and
the fire log (impulse_log) read/write.
"""
import json
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


# --- Source-impulse linkage (which capability fired a given session/outcome) ---
#
# The plugin's outcome POST carries only {profile, session_id, kind} — never the capability, because
# decide.txt hands the plugin the composed *prompt* and nothing else. So we bridge it here: at fire
# time we stash the just-fired label per (profile, class); the outcome endpoint reads it back to label
# its record (exact, going forward). For history recorded before this bridge existed, the panels fall
# back to `nearest_preceding_label` — a best-effort temporal match against the impulse_log ledger.

async def stash_last_fire(profile_name: str, cls: str, action: dict) -> None:
    """Remember what just fired, keyed by (profile, class), so the outcome POST can be labelled."""
    payload = json.dumps({"label": action.get("label") or action.get("id") or "", "at": time.time()})
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('impulse_last_fire', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (f"{profile_name}:{cls}", payload, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def last_fire_label(profile_name: str, cls: str) -> str:
    """The label of the most recent fire for (profile, class). '' if none stashed yet."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT value FROM module_data WHERE module = 'impulse_last_fire' AND key = ?",
            (f"{profile_name}:{cls}",),
        )
        row = await cur.fetchone()
    finally:
        await conn.close()
    if not row:
        return ""
    try:
        return json.loads(row[0]).get("label") or ""
    except (ValueError, TypeError):
        return ""


async def class_fires(profile_name: str, outward: bool) -> list[tuple]:
    """(fired_at, label) for one class, oldest→newest — the ledger used to temporally attribute
    pre-linkage history. Class is read off the stored prompt's OUTWARD_MARKER prefix."""
    op = "LIKE" if outward else "NOT LIKE"
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            f"SELECT fired_at, label, action_id FROM impulse_log "
            f"WHERE profile_name = ? AND prompt {op} '[IMPULSE-OUTWARD]%' ORDER BY fired_at",
            (profile_name,),
        )
        return [(r[0], r[1] or r[2] or "") for r in await cur.fetchall()]
    finally:
        await conn.close()


def nearest_preceding_label(fires: list, at: float, max_gap: float = 3600.0) -> str:
    """Best-effort: label of the fire most closely preceding `at` (a small forward tolerance absorbs
    clock skew), within `max_gap` seconds. '' when nothing fits — the row then shows '—'."""
    if not at:
        return ""
    best = None
    for f_at, label in fires:
        if f_at <= at + 120 and (best is None or f_at > best[0]):
            best = (f_at, label)
    if best and abs(at - best[0]) <= max_gap:
        return best[1]
    return ""


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


async def fire_overview(profile_name: str, hours: int = 24) -> dict:
    """At-a-glance summary of which impulses fired over the last `hours`, grouped by capability.
    Reads the impulse_log ledger (every fire, inward + outward). Class is inferred from the stored
    prompt's OUTWARD_MARKER prefix (only %/_ are LIKE-special in SQLite, so the literal matches)."""
    since_ts = time.time() - hours * 3600
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT action_id, label, COUNT(*) AS fires, MAX(fired_at) AS last_fired, "
            "SUM(CASE WHEN prompt LIKE '[IMPULSE-OUTWARD]%' THEN 1 ELSE 0 END) AS outward "
            "FROM impulse_log WHERE profile_name = ? AND fired_at >= ? "
            "GROUP BY action_id, label ORDER BY fires DESC, last_fired DESC",
            (profile_name, since_ts),
        )
        rows = await cursor.fetchall()
    finally:
        await conn.close()
    out_rows = [
        {"label": r[1] or r[0] or "—", "fires": r[2], "last_fired": r[3],
         "cls": "outward" if (r[4] or 0) > 0 else "inward"}
        for r in rows
    ]
    total = sum(r["fires"] for r in out_rows)
    outward = sum(r["fires"] for r in out_rows if r["cls"] == "outward")
    return {"rows": out_rows, "total": total, "outward": outward,
            "inward": total - outward, "hours": hours}


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
