import json
import time
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from pathlib import Path
from ..db.hermes import get_recent_sessions, get_session_stats
from ..db.ego import get_ego_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _parse_source(source_str: str | None) -> dict:
    if not source_str:
        return {}
    try:
        return json.loads(source_str)
    except Exception:
        return {}


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
        cutoff = time.time() - 600  # sessions with activity in the last 10 min
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
async def dashboard(request: Request):
    sessions = await get_recent_sessions()
    stats = await get_session_stats()
    platform_stats = await _get_platform_stats()
    gateway_startup = await _get_last_gateway_startup()
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()

    # Fetch sentiment for recent sessions in one query
    recent_ids = [s["id"] for s in sessions[:10]]
    sentiment_map: dict = {}
    if recent_ids:
        conn = await get_ego_db()
        try:
            placeholders = ",".join("?" * len(recent_ids))
            cursor = await conn.execute(
                f"SELECT key, value FROM module_data WHERE module='sentiment' AND key IN ({placeholders})",
                recent_ids,
            )
            for row in await cursor.fetchall():
                sentiment_map[row[0]] = json.loads(row[1])
        finally:
            await conn.close()

    recent = []
    for r in sessions[:10]:
        src = _parse_source(r.get("source"))
        s_data = sentiment_map.get(r["id"], {})
        recent.append({
            **r,
            "platform_name": src.get("platform", ""),
            "user_display": src.get("user_name") or src.get("user_id", ""),
            "sentiment_user":  s_data.get("user",  {}).get("dominant") if s_data else None,
            "sentiment_agent": s_data.get("agent", {}).get("dominant") if s_data else None,
        })

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
        },
    )


@router.get("/partials/activity")
async def activity_partial(request: Request):
    activity = await _get_activity_by_day()
    active_sessions = await _get_active_sessions()
    return templates.TemplateResponse(
        "partials/activity.html",
        {"request": request, "activity": activity, "active_sessions": active_sessions},
    )
