import json
import time
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from ..db.ego import get_ego_db
from ..db.hermes import get_sessions_by_ids
from ..services.conversations import get_all_recent_conversations
from ..services.profiles import discover_profiles

router = APIRouter(prefix="/api")


class TopicResult(BaseModel):
    session_id: str
    topic: Optional[str] = None
    mode: Optional[str] = None


@router.get("/topic/pending")
async def get_pending_sessions() -> list[dict]:
    conversations = await get_all_recent_conversations()
    if not conversations:
        return []
    conv_ids = [c["id"] for c in conversations]
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(conv_ids))
        cursor = await conn.execute(
            f"SELECT key FROM module_data WHERE module='topic' AND key IN ({ph})", conv_ids
        )
        already_topic = {row[0] for row in await cursor.fetchall()}
        cursor = await conn.execute(
            f"SELECT key FROM module_data WHERE module='mode' AND key IN ({ph})", conv_ids
        )
        already_mode = {row[0] for row in await cursor.fetchall()}
        already_both = already_topic & already_mode
    finally:
        await conn.close()
    pending = [c for c in conversations if c["id"] not in already_both]
    if not pending:
        return []

    # Batch-fetch cwd from hermes per profile so mode classification works
    db_by_profile = {p["name"]: p["db_path"] for p in discover_profiles()}
    by_profile: dict = {}
    for c in pending:
        by_profile.setdefault(c["profile_name"], []).append(c)
    cwd_map: dict = {}
    for pname, convs in by_profile.items():
        sids = list({c["session_id"] for c in convs})
        try:
            sessions = await get_sessions_by_ids(sids, db_path=db_by_profile.get(pname))
            for s in sessions:
                cwd_map[s["id"]] = s.get("cwd") or ""
        except Exception:
            pass

    return [
        {
            "session_id": c["id"],
            "title": c.get("title") or "",
            "cwd": cwd_map.get(c["session_id"], ""),
        }
        for c in pending
    ]


@router.post("/topic/score", status_code=202)
async def save_topic(result: TopicResult):
    now = time.time()
    # A conversation is "analyzed" only when it has BOTH topic and mode. If the
    # worker found a topic but couldn't map a mode (off-list word), default the
    # mode so the conversation can't get stuck perpetually "unanalyzed".
    mode = result.mode
    if result.topic is not None and not mode:
        mode = "social"
    conn = await get_ego_db()
    try:
        if result.topic is not None:
            await conn.execute(
                """
                INSERT INTO module_data (module, key, value, updated_at)
                VALUES ('topic', ?, ?, ?)
                ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (result.session_id, result.topic, now),
            )
        if mode is not None:
            await conn.execute(
                """
                INSERT INTO module_data (module, key, value, updated_at)
                VALUES ('mode', ?, ?, ?)
                ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (result.session_id, mode, now),
            )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "saved"}


@router.get("/mode/{session_id}")
async def get_session_mode(session_id: str) -> dict:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='mode' AND key=?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return {"mode": row[0]} if row else {}
    finally:
        await conn.close()


@router.get("/topic/status")
async def topic_status() -> dict:
    conversations = await get_all_recent_conversations()
    conv_ids = [c["id"] for c in conversations]

    conn = await get_ego_db()
    try:
        if conv_ids:
            ph = ",".join("?" * len(conv_ids))
            cursor = await conn.execute(
                f"SELECT key FROM module_data WHERE module='topic' AND key IN ({ph})", conv_ids
            )
            already_topic = {row[0] for row in await cursor.fetchall()}
            cursor = await conn.execute(
                f"SELECT key FROM module_data WHERE module='mode' AND key IN ({ph})", conv_ids
            )
            already_mode = {row[0] for row in await cursor.fetchall()}
        else:
            already_topic = set()
            already_mode = set()
        already_labeled = already_topic & already_mode

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='topic_trigger'"
        )
        row = await cursor.fetchone()
        triggered = row is not None and row[0] == "1"

        cursor = await conn.execute(
            "SELECT updated_at FROM module_data WHERE module='topic' ORDER BY updated_at DESC LIMIT 1"
        )
        last_row = await cursor.fetchone()
        last_run = last_row[0] if last_row else None

        cursor = await conn.execute(
            "SELECT updated_at FROM module_data WHERE module='_system' AND key='topic_heartbeat'"
        )
        hb_row = await cursor.fetchone()
        worker_online = hb_row is not None and (time.time() - hb_row[0]) < 90

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='topic_progress'"
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

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='topic_complete'"
        )
        complete_row = await cursor.fetchone()
        just_completed = (
            complete_row is not None
            and complete_row[0] == "1"
            and (time.time() - complete_row[1]) < 10
        )
        if just_completed:
            await conn.execute(
                "UPDATE module_data SET value='0' WHERE module='_system' AND key='topic_complete'"
            )
            await conn.commit()
    finally:
        await conn.close()

    return {
        "pending": len([c for c in conversations if c["id"] not in already_labeled]),
        "triggered": triggered,
        "last_run": last_run,
        "worker_online": worker_online,
        "progress": progress,
        "just_completed": just_completed,
    }


@router.post("/topic/trigger", status_code=202)
async def trigger_labeling():
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'topic_trigger', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
        )
        await conn.commit()
    finally:
        await conn.close()
    return JSONResponse({"status": "queued"}, headers={"HX-Trigger": "topicUpdate"})


@router.post("/topic/trigger-clear", status_code=202)
async def clear_trigger():
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE module_data SET value='0' WHERE module='_system' AND key='topic_trigger'"
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "cleared"}


@router.post("/topic/heartbeat", status_code=202)
async def worker_heartbeat():
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'topic_heartbeat', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "ok"}


@router.post("/topic/progress", status_code=202)
async def update_progress(current: int, total: int, session_id: str = ""):
    conn = await get_ego_db()
    try:
        value = json.dumps({"current": current, "total": total, "session_id": session_id})
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'topic_progress', ?, ?)
            ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (value, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "ok"}


@router.post("/topic/complete", status_code=202)
async def labeling_complete():
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', 'topic_complete', '1', ?)
            ON CONFLICT(module, key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (time.time(),),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "ok"}


@router.get("/topic/{session_id}")
async def get_session_topic(session_id: str) -> dict:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='topic' AND key=?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return {"topic": row[0]} if row else {}
    finally:
        await conn.close()
