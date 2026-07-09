import time
from fastapi import APIRouter, Request, Query
from fastapi.responses import Response, PlainTextResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services import impulse_engine, settings_store
from ..services.profiles import discover_profiles, resolve_profile

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

CLASSES = ("inward", "outward")
BACKING_KINDS = ("tool", "skill", "plugin-tool")


# --- Capability editor (the impulse "action" manifest — what the v2 arbiter may choose among) ---

async def _editor_ctx(profile: str) -> dict:
    from ..services.impulse_arbiter import _class_of
    caps = await settings_store._load_capabilities_raw()
    for c in caps:
        c["class"] = _class_of(c)  # normalize (fills legacy entries) for display
    caps.sort(key=lambda c: (c["class"], not c.get("enabled"), c.get("id", "")))
    return {
        "profiles": discover_profiles(), "active_profile": profile,
        "capabilities": caps, "classes": CLASSES, "backing_kinds": BACKING_KINDS,
        "log": await impulse_engine.get_recent_log(profile),
    }


@router.get("/impulses")
async def impulses_page(request: Request, profile: str = "default"):
    ctx = await _editor_ctx(profile)
    ctx["request"] = request
    return templates.TemplateResponse("impulses.html", ctx)


@router.get("/partials/capability-list")
async def capability_list_partial(request: Request, profile: str = "default"):
    ctx = await _editor_ctx(profile)
    ctx["request"] = request
    return templates.TemplateResponse("partials/capability_list.html", ctx)


def _parse_capability(form) -> dict:
    return {
        "id": str(form.get("id", "")),
        "class": str(form.get("class", "inward")),
        "intent": str(form.get("intent", "")),
        "backing_kind": str(form.get("backing_kind", "tool")),
        "skill": str(form.get("skill", "")),
        "description": str(form.get("description", "")),
        "operational_hint": str(form.get("operational_hint", "")),
        "enabled": str(form.get("enabled", "")) in ("on", "true", "1"),
    }


async def _capability_list_response(request: Request, profile: str):
    ctx = await _editor_ctx(profile)
    ctx["request"] = request
    return templates.TemplateResponse("partials/capability_list.html", ctx)


@router.post("/api/capabilities")
async def upsert_capability_ep(request: Request):
    form = await request.form()
    profile = str(form.get("profile", "default"))
    try:
        await settings_store.upsert_capability(_parse_capability(form))
    except ValueError as exc:
        return HTMLResponse(f"<p style='color:#e57373; font-size:0.82rem;'>⚠ {exc}</p>", status_code=400)
    return await _capability_list_response(request, profile)


@router.patch("/api/capabilities/{cid}/toggle")
async def toggle_capability_ep(request: Request, cid: str, profile: str = "default"):
    await settings_store.toggle_capability(cid)
    return await _capability_list_response(request, profile)


@router.delete("/api/capabilities/{cid}")
async def delete_capability_ep(request: Request, cid: str, profile: str = "default"):
    await settings_store.delete_capability(cid)
    return await _capability_list_response(request, profile)


# --- Live decision preview (v2 arbiter dry-run — "what would this agent do right now, and why") ---

@router.get("/partials/impulse-decide")
async def impulse_decide_partial(request: Request, profile: str = "default"):
    from ..services import impulse_arbiter
    db_path = resolve_profile(profile)
    decisions = {}
    for cls in CLASSES:
        try:
            decisions[cls] = await impulse_arbiter.arbitrate(profile, cls, db_path=db_path, commit=False)
        except Exception as exc:
            decisions[cls] = {"fired": False, "reason": f"error: {exc}", "capability_id": None, "prompt": None}
    return templates.TemplateResponse(
        "partials/impulse_decide.html", {"request": request, "decisions": decisions, "active_profile": profile})


@router.get("/partials/impulse-log")
async def impulse_log_partial(request: Request, profile: str = "default"):
    log = await impulse_engine.get_recent_log(profile)
    return templates.TemplateResponse("partials/impulse_log.html", {"request": request, "log": log})


# --- Impulse v2: brief (plugin pre_llm_call) + the arbiter decision (cron target) + outcome ---

async def _den_affinity_block(profile: str) -> str:
    """A short 'what's been on your mind' block for the inward brief — recent Den meaning + interests,
    replacing the absent mnemosyne recall (targeted recall injection is still deferred)."""
    from ..services import den
    from ..services.affinity_engine import get_taste_context
    lines: list[str] = []
    try:
        entries = den.list_entries(profile)[:3]
    except Exception:
        entries = []
    den_lines = [f"{e['date']}: {e['summary'] or e['slug']}" for e in entries if (e.get("summary") or e.get("slug"))]
    if den_lines:
        lines.append("Recently in your Den:\n- " + "\n- ".join(den_lines))
    try:
        taste = await get_taste_context(profile, sample=True)
    except Exception:
        taste = {}
    if taste.get("interests") and taste["interests"] != "—":
        lines.append(f"You've been drawn to: {taste['interests']}.")
    return "\n\n".join(lines)


@router.get("/api/impulse/brief")
async def impulse_brief(profile: str = "default", kind: str = "inward") -> dict:
    """Context brief for an impulse agent turn, injected by the plugin's pre_llm_call hook (the cron
    session is cold — no mnemosyne, no live history). Mood directive always; recent-conversation gist
    for outward; recent Den/affinity context for inward (stands in for the absent recall)."""
    from ..services.mood_engine import get_cached_mood
    from ..services import settings_store as _ss
    from ..services.impulse_arbiter import recent_gist
    parts: list[str] = []
    mood = await get_cached_mood(profile)
    if mood:
        tmpl = await _ss.get_setting("mood_directive_template", "") or ""
        directive = tmpl.replace("{mood}", mood["name"]).replace("{description}", mood.get("description", "")).strip()
        if directive:
            parts.append(directive)
    if kind == "outward":
        gist = await recent_gist(profile)
        if gist:
            parts.append("Where things stand with the user right now (recent messages):\n" + gist)
    else:
        ctx = await _den_affinity_block(profile)
        if ctx:
            parts.append(ctx)
    return {"profile": profile, "kind": kind, "context": "\n\n".join(parts)}


@router.get("/api/impulse/decide")
async def impulse_decide_json(profile: str = "default", cls: str = Query("inward", alias="class")) -> dict:
    """Dry-run arbiter decision for the dashboard/testing (no fire logged)."""
    from ..services import impulse_arbiter
    return await impulse_arbiter.arbitrate(profile, cls, db_path=resolve_profile(profile), commit=False)


@router.get("/api/impulse/decide.txt", response_class=PlainTextResponse)
async def impulse_decide_txt(profile: str = "default", cls: str = Query("inward", alias="class")):
    """Plain-text relay for the cron check-in scripts: the composed impulse prompt, or EMPTY when the
    arbiter stays quiet. Empty stdout makes Hermes skip the agent entirely. Commits the fire."""
    from ..services import impulse_arbiter
    result = await impulse_arbiter.arbitrate(profile, cls, db_path=resolve_profile(profile), commit=True)
    return PlainTextResponse(result["prompt"] if result["fired"] else "")


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
