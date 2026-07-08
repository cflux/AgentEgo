import time
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from pathlib import Path
from ..db.hermes import get_session_stats, get_all_session_stats
from ..db.ego import get_ego_db
from ..services.profiles import discover_profiles, resolve_profile
from ..services.conversations import sync_recent_conversations
from .sentiment import scoring_status
from .topic import topic_status

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


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
        return row[0] if row else None  # raw ts; rendered via the `ts` filter in the template
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
        stats = await get_all_session_stats()
    else:
        await sync_recent_conversations(profile, db_path)
        stats = await get_session_stats(db_path=db_path)

    platform_stats = await _get_platform_stats()
    gateway_startup = await _get_last_gateway_startup()
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()
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


@router.get("/partials/activity")
async def activity_partial(request: Request):
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()
    return templates.TemplateResponse(
        "partials/activity.html",
        {"request": request, "activity": activity, "active_sessions": active_sessions},
    )
