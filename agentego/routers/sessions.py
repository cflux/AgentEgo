import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.hermes import (
    get_session, find_session,
    get_session_messages_in_range, find_session_messages,
    get_sessions_by_ids,
)
from ..db.ego import get_ego_db
from ..services.profiles import discover_profiles, resolve_profile
from ..services.conversations import (
    sync_recent_conversations, get_recent_conversations, get_all_recent_conversations,
    get_conversation, get_first_conv_id_for_session,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _get_topics_batch(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(keys))
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='topic' AND key IN ({ph})", keys
        )
        return {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await conn.close()


async def _get_modes_batch(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(keys))
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='mode' AND key IN ({ph})", keys
        )
        return {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await conn.close()


async def _get_sentiment(key: str) -> dict:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='sentiment' AND key=?", (key,)
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        await conn.close()


async def _get_topic(key: str) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='topic' AND key=?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def _get_mode(key: str) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='mode' AND key=?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


def _parse_source(source_str: str | None) -> dict:
    if not source_str:
        return {}
    try:
        return json.loads(source_str)
    except Exception:
        return {"platform": source_str}


@router.get("/sessions")
async def sessions_page(request: Request, platform: str = "", user_id: str = "", profile: str = ""):
    profiles = discover_profiles()
    multi = not profile

    # Lazily sync conversations for any new sessions
    if profile:
        db_path = resolve_profile(profile)
        await sync_recent_conversations(profile, db_path)
        conversations = await get_recent_conversations(profile)
    else:
        for p in profiles:
            await sync_recent_conversations(p["name"], p["db_path"])
        conversations = await get_all_recent_conversations()

    # Batch-fetch parent session metadata grouped by profile
    session_meta: dict[str, dict] = {}
    by_profile: dict[str, list[str]] = {}
    for conv in conversations:
        by_profile.setdefault(conv["profile_name"], []).append(conv["session_id"])

    for pname, sids in by_profile.items():
        dp = resolve_profile(pname)
        if dp:
            try:
                for s in await get_sessions_by_ids(list(set(sids)), db_path=dp):
                    session_meta[s["id"]] = s
            except Exception:
                pass

    # Merge and filter
    enriched = []
    for conv in conversations:
        s = session_meta.get(conv["session_id"], {})
        src = _parse_source(s.get("source"))
        plat = src.get("platform", s.get("platform", "")) or "console"
        uid = src.get("user_id", s.get("user_id", ""))
        if platform and plat != platform:
            continue
        if user_id and uid != user_id:
            continue
        enriched.append({
            **conv,
            "platform_name": plat,
            "user_display": uid,
            "model": s.get("model", ""),
            "input_tokens": s.get("input_tokens", 0),
            "output_tokens": s.get("output_tokens", 0),
            "estimated_cost_usd": s.get("estimated_cost_usd", 0),
            "end_reason": s.get("end_reason", "active"),
            "ended_at": s.get("ended_at"),
        })

    # Keep a session's parts together and in order: most-recently-active session
    # first, then Part 1, 2, 3… within each session.
    recency: dict = {}
    for c in enriched:
        key = (c["profile_name"], c["session_id"])
        recency[key] = max(recency.get(key, 0.0), c.get("end_ts") or 0.0)
    enriched.sort(key=lambda c: (
        -recency[(c["profile_name"], c["session_id"])],
        c["session_id"],
        c.get("part_index") or 0,
    ))
    # Mark the first row of each session so the table can separate groups visually.
    prev_key = None
    for c in enriched:
        key = (c["profile_name"], c["session_id"])
        c["is_session_start"] = key != prev_key
        prev_key = key

    conv_ids = [c["id"] for c in enriched]
    topics = await _get_topics_batch(conv_ids)
    modes = await _get_modes_batch(conv_ids)
    for c in enriched:
        c["topic"] = topics.get(c["id"])
        c["mode"] = modes.get(c["id"])

    platforms = sorted({c["platform_name"] for c in enriched if c["platform_name"]})
    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "sessions": enriched,
            "platforms": platforms,
            "filter_platform": platform,
            "filter_user": user_id,
            "profiles": profiles,
            "active_profile": profile,
            "multi_profile": multi,
        },
    )


@router.get("/conversations/{conv_id}")
async def conversation_detail(request: Request, conv_id: str):
    conv = await get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    profile_name = conv["profile_name"]
    db_path = resolve_profile(profile_name)
    session = await get_session(conv["session_id"], db_path=db_path) if db_path else None
    if not session:
        session, profile_name = await find_session(conv["session_id"])
        db_path = resolve_profile(profile_name) if profile_name else None

    if not session:
        raise HTTPException(status_code=404, detail="Parent session not found")

    messages = await get_session_messages_in_range(
        conv["session_id"], conv["start_ts"], conv["end_ts"], db_path=db_path
    )

    src = _parse_source(session.get("source"))
    session["platform_name"] = src.get("platform") or "console"
    session["user_display"] = src.get("user_name") or src.get("user_id", "")
    session["chat_name"] = src.get("chat_name", src.get("chat_id", ""))
    session["profile_name"] = profile_name

    sentiment = await _get_sentiment(conv_id)
    topic = await _get_topic(conv_id)
    mode = await _get_mode(conv_id)

    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "conv": conv,
            "messages": messages,
            "sentiment": sentiment,
            "topic": topic,
            "mode": mode,
        },
    )


@router.get("/sessions/{session_id}")
async def session_redirect(session_id: str, profile: str = ""):
    """Backward-compat redirect: old session URLs → first conversation of that session."""
    conv_id = await get_first_conv_id_for_session(session_id)
    if conv_id:
        return RedirectResponse(f"/conversations/{conv_id}", status_code=302)
    # No conversations synced yet — try to find the session and sync it first
    from ..db.hermes import get_session as _get_session
    db_path = resolve_profile(profile) if profile else None
    if db_path:
        s = await _get_session(session_id, db_path=db_path)
        if s:
            from ..services.conversations import sync_session_conversations
            await sync_session_conversations(s, profile or "default", db_path=db_path)
            conv_id = await get_first_conv_id_for_session(session_id)
            if conv_id:
                return RedirectResponse(f"/conversations/{conv_id}", status_code=302)
    raise HTTPException(status_code=404, detail="Session not found")
