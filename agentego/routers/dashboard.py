import json
import time
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from pathlib import Path
from ..db.hermes import get_session_stats, get_all_session_stats, get_sessions_by_ids
from ..db.ego import get_ego_db
from ..services.profiles import discover_profiles, resolve_profile
from ..services.conversations import (
    sync_recent_conversations, get_recent_conversations, get_all_recent_conversations,
)
from .sentiment import scoring_status
from .topic import topic_status

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_SKIP_EMOTIONS = {"neutral"}


def _first_non_neutral(sdata: dict) -> str | None:
    dominant = sdata.get("dominant")
    if dominant and dominant not in _SKIP_EMOTIONS:
        return dominant
    for e in sdata.get("top3") or []:
        if e not in _SKIP_EMOTIONS:
            return e
    return None


def _parse_source(source_str: str | None) -> dict:
    if not source_str:
        return {}
    try:
        return json.loads(source_str)
    except Exception:
        return {"platform": source_str}


async def _enrich_conversations(conversations: list) -> list:
    """Add sentiment, topic, mode, and session metadata to conversation dicts."""
    if not conversations:
        return []
    conv_ids = [c["id"] for c in conversations]
    sentiment_map: dict = {}
    topic_map: dict = {}
    mode_map: dict = {}
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(conv_ids))
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='sentiment' AND key IN ({ph})", conv_ids
        )
        for row in await cursor.fetchall():
            try:
                sentiment_map[row[0]] = json.loads(row[1])
            except Exception:
                pass
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='topic' AND key IN ({ph})", conv_ids
        )
        for row in await cursor.fetchall():
            topic_map[row[0]] = row[1]
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='mode' AND key IN ({ph})", conv_ids
        )
        for row in await cursor.fetchall():
            mode_map[row[0]] = row[1]
    finally:
        await conn.close()

    # Batch-fetch session metadata grouped by profile
    session_meta: dict[str, dict] = {}
    by_profile: dict[str, list[str]] = {}
    for c in conversations:
        by_profile.setdefault(c["profile_name"], []).append(c["session_id"])
    for pname, sids in by_profile.items():
        dp = resolve_profile(pname)
        if dp:
            try:
                for s in await get_sessions_by_ids(list(set(sids)), db_path=dp):
                    session_meta[s["id"]] = s
            except Exception:
                pass

    result = []
    for c in conversations:
        s = session_meta.get(c["session_id"], {})
        src = _parse_source(s.get("source"))
        s_data = sentiment_map.get(c["id"]) or {}
        result.append({
            **c,
            "platform_name": src.get("platform") or "console",
            "user_display": src.get("user_name") or src.get("user_id", ""),
            "model": s.get("model", ""),
            "sentiment_user": _first_non_neutral(s_data.get("user") or {}),
            "sentiment_agent": _first_non_neutral(s_data.get("agent") or {}),
            "topic": topic_map.get(c["id"]),
            "mode": mode_map.get(c["id"]),
        })
    return result


async def _get_platform_stats() -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT platform, SUM(session_count) AS sessions, SUM(agent_turn_count) AS turns
            FROM platform_stats
            GROUP BY platform
            ORDER BY sessions DESC
            """
        )
        rows = await cursor.fetchall()
        return [{"platform": r[0], "sessions": r[1], "turns": r[2]} for r in rows]
    finally:
        await conn.close()


async def _get_last_gateway_startup() -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT received_at FROM events
            WHERE event_type = 'gateway:startup'
            ORDER BY received_at DESC LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if row:
            import datetime
            return datetime.datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S")
        return None
    finally:
        await conn.close()


async def _get_activity_by_day() -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT date(received_at, 'unixepoch') AS d, COUNT(*) AS turns
            FROM events
            WHERE event_type = 'agent:start'
            GROUP BY d
            ORDER BY d DESC
            LIMIT 7
            """
        )
        rows = await cursor.fetchall()
        return [{"date": r[0], "turns": r[1]} for r in reversed(rows)]
    finally:
        await conn.close()


async def _get_active_sessions() -> int:
    conn = await get_ego_db()
    try:
        cutoff = time.time() - 600
        cursor = await conn.execute(
            """
            SELECT COUNT(DISTINCT session_id) FROM events
            WHERE event_type = 'agent:start' AND received_at >= ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()


@router.get("/")
async def dashboard(request: Request, profile: str = ""):
    profiles = discover_profiles()
    db_path = resolve_profile(profile) if profile else None
    multi = not profile

    if multi:
        for p in profiles:
            await sync_recent_conversations(p["name"], p["db_path"])
        conversations = await get_all_recent_conversations()
        stats = await get_all_session_stats()
    else:
        await sync_recent_conversations(profile, db_path)
        conversations = await get_recent_conversations(profile)
        stats = await get_session_stats(db_path=db_path)

    platform_stats = await _get_platform_stats()
    gateway_startup = await _get_last_gateway_startup()
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()
    recent = await _enrich_conversations(conversations[:10])
    status = await scoring_status()
    topic_status_data = await topic_status()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "platform_stats": platform_stats,
            "gateway_startup": gateway_startup,
            "activity": activity,
            "active_sessions": active_sessions,
            "recent_sessions": recent,
            "status": status,
            "topic_status": topic_status_data,
            "profiles": profiles,
            "active_profile": profile,
            "multi_profile": multi,
        },
    )


@router.get("/partials/sentiment-status")
async def sentiment_status_partial(request: Request):
    status = await scoring_status()
    headers = {"HX-Trigger": "sentimentComplete"} if status.get("just_completed") else {}
    return templates.TemplateResponse(
        "partials/sentiment_status.html",
        {"request": request, "status": status},
        headers=headers,
    )


@router.get("/partials/topic-status")
async def topic_status_partial(request: Request):
    status = await topic_status()
    headers = {"HX-Trigger": "topicComplete"} if status.get("just_completed") else {}
    return templates.TemplateResponse(
        "partials/topic_status.html",
        {"request": request, "status": status},
        headers=headers,
    )


@router.get("/partials/recent-sessions")
async def recent_sessions_partial(request: Request, profile: str = ""):
    db_path = resolve_profile(profile) if profile else None
    multi = not profile
    if multi:
        conversations = await get_all_recent_conversations()
    else:
        conversations = await get_recent_conversations(profile)
    recent = await _enrich_conversations(conversations[:10])
    return templates.TemplateResponse(
        "partials/recent_sessions.html",
        {"request": request, "recent_sessions": recent, "multi_profile": multi},
    )


@router.get("/partials/activity")
async def activity_partial(request: Request):
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()
    return templates.TemplateResponse(
        "partials/activity.html",
        {"request": request, "activity": activity, "active_sessions": active_sessions},
    )
