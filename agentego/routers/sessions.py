import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from pathlib import Path
from ..db.hermes import get_recent_sessions, get_session, get_session_messages
from ..db.ego import get_ego_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _get_sentiment(session_id: str) -> dict:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module = 'sentiment' AND key = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        await conn.close()


def _parse_source(source_str: str | None) -> dict:
    if not source_str:
        return {}
    try:
        return json.loads(source_str)
    except Exception:
        return {}


@router.get("/sessions")
async def sessions_page(request: Request, platform: str = "", user_id: str = ""):
    rows = await get_recent_sessions()
    sessions = []
    for r in rows:
        src = _parse_source(r.get("source"))
        plat = src.get("platform", r.get("platform", ""))
        uid = src.get("user_id", r.get("user_id", ""))
        plat = plat or "console"
        if platform and plat != platform:
            continue
        if user_id and uid != user_id:
            continue
        sessions.append({**r, "platform_name": plat, "user_display": uid})

    platforms = sorted({s["platform_name"] for s in sessions if s["platform_name"]})
    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "sessions": sessions,
            "platforms": platforms,
            "filter_platform": platform,
            "filter_user": user_id,
        },
    )


@router.get("/sessions/{session_id}")
async def session_detail(request: Request, session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = await get_session_messages(session_id)
    src = _parse_source(session.get("source"))
    session["platform_name"] = src.get("platform") or "console"
    session["user_display"] = src.get("user_name") or src.get("user_id", "")
    session["chat_name"] = src.get("chat_name", src.get("chat_id", ""))
    sentiment = await _get_sentiment(session_id)
    return templates.TemplateResponse(
        "session_detail.html",
        {"request": request, "session": session, "messages": messages, "sentiment": sentiment},
    )
