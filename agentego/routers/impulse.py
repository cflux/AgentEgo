import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import Response, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services import impulse_engine
from ..services.profiles import discover_profiles, resolve_profile

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# --- Helpers ---

async def _get_moods() -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT id, name, color, icon FROM moods ORDER BY name")
        return [{"id": r[0], "name": r[1], "color": r[2], "icon": r[3]} for r in await cursor.fetchall()]
    finally:
        await conn.close()


def _mood_map(moods: list) -> dict:
    return {m["id"]: m for m in moods}


def _action_summary(action: dict) -> str:
    moods = action.get("required_moods") or []
    if not moods:
        mood_part = "any mood"
    elif action.get("mood_negate"):
        mood_part = "while NOT " + "/".join(moods)
    else:
        mood_part = "while " + "/".join(moods)
    idle = action.get("min_idle_minutes", 0)
    idle_part = "any idle" if not idle else f"idle ≥ {idle}m"
    return f"{mood_part} · {idle_part} · weight {action.get('base_weight', 1)}"


def _parse_moods(form) -> list:
    raw = form.getlist("required_moods") if hasattr(form, "getlist") else []
    if isinstance(raw, str):
        raw = [raw]
    return [m for m in raw if m]


# --- Page ---

@router.get("/impulses")
async def impulses_page(request: Request, profile: str = "default"):
    moods = await _get_moods()
    actions = await impulse_engine.list_actions(profile)
    for a in actions:
        a["summary"] = _action_summary(a)
    db_path = resolve_profile(profile)
    preview = await impulse_engine.evaluate_impulse(profile, db_path=db_path, commit=False)
    log = await impulse_engine.get_recent_log(profile)
    return templates.TemplateResponse(
        "impulses.html",
        {
            "request": request,
            "profiles": discover_profiles(),
            "active_profile": profile,
            "moods": moods,
            "mood_map": _mood_map(moods),
            "actions": actions,
            "preview": preview,
            "log": log,
        },
    )


# --- CRUD ---

@router.post("/api/impulses")
async def create_impulse(request: Request):
    form = await request.form()
    profile_name = str(form.get("profile_name", "default"))
    label = str(form.get("label", "")).strip() or "Untitled impulse"
    prompt = str(form.get("prompt", "")).strip()
    required_moods = _parse_moods(form)
    mood_negate = str(form.get("mood_match", "any")) == "not"
    min_idle = int(form.get("min_idle_minutes", 0) or 0)
    base_weight = float(form.get("base_weight", 1.0) or 1.0)
    recency = int(form.get("recency_window_minutes", 240) or 240)
    if prompt:
        await impulse_engine.create_action(profile_name, label, prompt, required_moods,
                                            min_idle, base_weight, recency, mood_negate)
    return RedirectResponse(f"/impulses?profile={profile_name}", status_code=303)


@router.get("/api/impulses/{action_id}/edit-form")
async def impulse_edit_form(request: Request, action_id: str):
    action = await impulse_engine.get_action(action_id)
    if not action:
        return Response(status_code=404)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/impulse_edit_row.html",
        {"request": request, "action": action, "moods": moods},
    )


@router.post("/api/impulses/{action_id}/edit")
async def edit_impulse(request: Request, action_id: str):
    form = await request.form()
    label = str(form.get("label", "")).strip() or "Untitled impulse"
    prompt = str(form.get("prompt", "")).strip()
    required_moods = _parse_moods(form)
    mood_negate = str(form.get("mood_match", "any")) == "not"
    min_idle = int(form.get("min_idle_minutes", 0) or 0)
    base_weight = float(form.get("base_weight", 1.0) or 1.0)
    recency = int(form.get("recency_window_minutes", 240) or 240)
    await impulse_engine.update_action(action_id, label, prompt, required_moods,
                                       min_idle, base_weight, recency, mood_negate)
    action = await impulse_engine.get_action(action_id)
    action["summary"] = _action_summary(action)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/impulse_row.html",
        {"request": request, "action": action, "mood_map": _mood_map(moods)},
    )


@router.get("/api/impulses/{action_id}/row")
async def impulse_row(request: Request, action_id: str):
    action = await impulse_engine.get_action(action_id)
    if not action:
        return Response(status_code=404)
    action["summary"] = _action_summary(action)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/impulse_row.html",
        {"request": request, "action": action, "mood_map": _mood_map(moods)},
    )


@router.delete("/api/impulses/{action_id}")
async def delete_impulse(action_id: str):
    await impulse_engine.delete_action(action_id)
    return Response(status_code=200)


@router.patch("/api/impulses/{action_id}/toggle")
async def toggle_impulse(action_id: str):
    await impulse_engine.toggle_action(action_id)
    return Response(status_code=200)


# --- Check-in endpoints (the Hermes cron target) ---

@router.get("/api/impulse/next")
async def impulse_next(profile: str = "default") -> dict:
    """JSON decision. Commits the fire (updates recency + logs)."""
    result = await impulse_engine.evaluate_impulse(profile, db_path=resolve_profile(profile), commit=True)
    return {
        "fired": result["fired"],
        "prompt": result["prompt"],
        "action": result["action"],
        "mood": result["mood"]["id"] if result["mood"] else None,
        "idle_minutes": result["idle_minutes"],
    }


@router.get("/api/impulse/next.txt", response_class=PlainTextResponse)
async def impulse_next_txt(profile: str = "default"):
    """Plain-text relay for the cron script: the impulse prompt, or EMPTY when no
    impulse fires. Empty stdout makes Hermes skip the agent entirely (no LLM call)."""
    result = await impulse_engine.evaluate_impulse(profile, db_path=resolve_profile(profile), commit=True)
    return PlainTextResponse(result["prompt"] if result["fired"] else "")


# --- Impulse v2: brief (consumed by the plugin pre_llm_call) + outcome (from post_llm_call) ---

def _is_cron_session(s: dict) -> bool:
    """Skip our own sidequest/cron sessions — they aren't 'conversations with the user'."""
    if str(s.get("id") or "").startswith("cron_"):
        return True
    src = s.get("source") or ""
    if "cron" in str(src).lower():
        return True
    return False


async def _recent_gist(profile: str, max_turns: int = 8) -> str:
    """A short recent user/agent transcript for briefing an outward impulse (the cron turn is cold).
    Excludes cron/sidequest sessions so it reflects the actual conversation with the user."""
    from ..db.hermes import get_recent_sessions, get_session_messages
    db_path = resolve_profile(profile)
    try:
        sessions = [s for s in await get_recent_sessions(db_path=db_path) if not _is_cron_session(s)]
        if not sessions:
            return ""
        msgs = await get_session_messages(sessions[0]["id"], db_path=db_path)
    except Exception:
        return ""
    turns = [m for m in msgs if m.get("role") in ("user", "assistant") and m.get("content")]
    return "\n".join(f"{m['role']}: {m['content'][:300]}" for m in turns[-max_turns:])


@router.get("/api/impulse/brief")
async def impulse_brief(profile: str = "default", kind: str = "inward") -> dict:
    """Context brief for an impulse agent turn, injected by the plugin's pre_llm_call hook (the cron
    session is cold — no mnemosyne, no live history). Phase-1: mood directive always; recent-
    conversation gist for outward. Richer Den/affinity/recall context lands in later phases."""
    from ..services.mood_engine import get_cached_mood
    from ..services import settings_store
    parts: list[str] = []
    mood = await get_cached_mood(profile)
    if mood:
        tmpl = await settings_store.get_setting("mood_directive_template", "") or ""
        directive = tmpl.replace("{mood}", mood["name"]).replace("{description}", mood.get("description", "")).strip()
        if directive:
            parts.append(directive)
    if kind == "outward":
        gist = await _recent_gist(profile)
        if gist:
            parts.append("Where things stand with the user right now (recent messages):\n" + gist)
    return {"profile": profile, "kind": kind, "context": "\n\n".join(parts)}


@router.post("/api/impulse/outcome")
async def impulse_outcome(request: Request) -> dict:
    """Receive a sidequest outcome from the plugin's post_llm_call hook. Phase-1 stub: persist it;
    the sidequest scorer (agent-solo emotion → mood) arrives in Phase 3."""
    import json as _json
    from uuid import uuid4
    body = await request.json()
    profile = str(body.get("profile") or "default")
    session_id = str(body.get("session_id") or "")
    record = {
        "profile": profile,
        "label": str(body.get("label") or ""),
        "kind": str(body.get("kind") or ""),
        "response": str(body.get("response") or ""),
        "at": time.time(),
    }
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('impulse_outcome', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (session_id or str(uuid4()), _json.dumps(record), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"ok": True, "stored": True}


# --- Dry-run / dashboard ---

@router.get("/api/impulse/preview")
async def impulse_preview_json(profile: str = "default") -> dict:
    return await impulse_engine.evaluate_impulse(profile, db_path=resolve_profile(profile), commit=False)


@router.get("/partials/impulse-preview")
async def impulse_preview_partial(request: Request, profile: str = "default"):
    preview = await impulse_engine.evaluate_impulse(profile, db_path=resolve_profile(profile), commit=False)
    return templates.TemplateResponse(
        "partials/impulse_preview.html", {"request": request, "preview": preview}
    )


@router.get("/partials/impulse-log")
async def impulse_log_partial(request: Request, profile: str = "default"):
    log = await impulse_engine.get_recent_log(profile)
    return templates.TemplateResponse(
        "partials/impulse_log.html", {"request": request, "log": log}
    )
