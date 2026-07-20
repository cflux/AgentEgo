import logging
import time
from fastapi import APIRouter, Request, Query
from fastapi.responses import Response, PlainTextResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services import impulse_engine, settings_store
from ..services.profiles import discover_profiles, resolve_profile

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

CLASSES = ("inward", "outward")
BACKING_KINDS = ("tool", "skill", "plugin-tool")


# --- Capability editor (the impulse "action" manifest — what the v2 arbiter may choose among) ---

async def _editor_ctx(profile: str) -> dict:
    from ..services.impulse_arbiter import _class_of
    from ..services import mood_engine
    caps = await settings_store._load_capabilities_raw()
    for c in caps:
        c["class"] = _class_of(c)  # normalize (fills legacy entries) for display
    caps.sort(key=lambda c: (c["class"], not c.get("enabled"), c.get("id", "")))
    moods = sorted((await mood_engine._load_moods()).values(), key=lambda m: m.get("name") or m["id"])
    return {
        "profiles": discover_profiles(), "active_profile": profile,
        "capabilities": caps, "classes": CLASSES, "backing_kinds": BACKING_KINDS,
        "moods": moods,
        "log": await impulse_engine.get_recent_log(profile),
        "settings": await settings_store.get_all_settings(),
    }


# Editable impulse/solitude/pacing knobs (Phase 6). Grouped by handling so the save can coerce correctly.
_TUNING_CHECKBOX = ["impulse_enabled", "impulse_act_gate_enabled", "solitude_enabled"]
_TUNING_NUMERIC = [
    "impulse_inward_hour_start", "impulse_inward_hour_end",
    "impulse_outward_hour_start", "impulse_outward_hour_end",
    "impulse_outward_min_idle_minutes", "impulse_outward_backoff_cap_min",
    "impulse_act_probability_default",
    "solitude_onset_min", "solitude_rate_per_hour", "solitude_cap",
    "solitude_ignored_bump", "solitude_ignored_halflife_hours",
    "mood_solo_positive_weight", "mood_solo_negative_weight",
]
_TUNING_JSON = ["impulse_act_probability", "impulse_action_daily_budget", "solitude_targets"]


@router.post("/api/impulse/tuning")
async def update_tuning(request: Request):
    import json as _json
    form = await request.form()
    profile = str(form.get("profile") or "default")
    updates: dict = {}
    for k in _TUNING_CHECKBOX:
        updates[k] = "1" if form.get(k) else "0"
    for k in _TUNING_NUMERIC:
        v = str(form.get(k) or "").strip()
        if v:
            updates[k] = v
    for k in _TUNING_JSON:  # only overwrite on valid JSON (mirrors config_panel's cascade guard)
        v = str(form.get(k) or "").strip()
        if v:
            try:
                _json.loads(v)
                updates[k] = v
            except ValueError:
                pass
    await settings_store.set_settings(updates)
    ctx = {"request": request, "active_profile": profile, "saved": True,
           "settings": await settings_store.get_all_settings()}
    return templates.TemplateResponse("partials/impulse_tuning.html", ctx)


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
        "mood_tags": form.getlist("mood_tags"),  # multi-value — getlist, not get
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


# --- Dashboard v2 (Phase 6): status strip, recent sidequests, reach-out attribution ---

async def _status_ctx(profile: str) -> dict:
    from ..services.mood_engine import get_cached_mood
    from ..services.impulse_engine import get_last_activity_ts
    from ..services.solitude import solitude_votes
    from ..services.impulse_feedback import consecutive_ignored, attribution_summary
    db_path = resolve_profile(profile)
    mood = await get_cached_mood(profile)
    last = await get_last_activity_ts(profile, db_path=db_path)
    idle_min = (time.time() - last) / 60.0 if last else None
    _, sol_note = await solitude_votes(profile, db_path=db_path)
    return {
        "mood": mood, "idle_min": idle_min, "solitude": sol_note,
        "consecutive_ignored": await consecutive_ignored(profile, db_path=db_path),
        "attribution": await attribution_summary(profile, db_path=db_path),
    }


@router.get("/partials/impulse-status")
async def impulse_status_partial(request: Request, profile: str = "default"):
    ctx = await _status_ctx(profile)
    ctx["request"] = request
    return templates.TemplateResponse("partials/impulse_status.html", ctx)


async def _outcome_labels(profile: str, kind: str) -> dict:
    """{session_id: source-impulse label} from the impulse_outcome records for a class. Lets the
    sidequest panel show which impulse fired a round (solo_round & impulse_outcome share the key)."""
    import json as _json
    conn = await get_ego_db()
    try:
        cur = await conn.execute("SELECT key, value FROM module_data WHERE module = 'impulse_outcome'")
        rows = await cur.fetchall()
    finally:
        await conn.close()
    labels: dict = {}
    for key, val in rows:
        try:
            rec = _json.loads(val)
        except (ValueError, TypeError):
            continue
        if rec.get("profile") == profile and rec.get("kind") == kind:
            labels[key] = rec.get("label") or ""
    return labels


@router.get("/partials/solo-rounds")
async def solo_rounds_partial(request: Request, profile: str = "default"):
    from ..services.sidequest_scorer import recent_solo_enriched
    rounds = await recent_solo_enriched(profile, limit=12)
    label_map = await _outcome_labels(profile, "inward")
    items = []
    for r in rounds:
        emo = r.get("agent_scores") or {}
        mood = r.get("mood_scores") or {}
        key = str(r.get("id") or "").split(":", 1)[-1]
        items.append({
            "subject": r.get("topic") or "—",
            "source": label_map.get(key) or "",
            "valence": r.get("valence", 0.0),
            "emotion": max(emo, key=emo.get) if emo else None,
            "mood": max(mood, key=mood.get) if mood else None,
            "at": r.get("end_ts"),
        })
    # Backfill the source impulse for rounds recorded before the fire→outcome label bridge existed.
    if any(not it["source"] for it in items):
        fires = await impulse_engine.class_fires(profile, outward=False)
        for it in items:
            if not it["source"]:
                it["source"] = impulse_engine.nearest_preceding_label(fires, it["at"] or 0)
    for it in items:
        it["source"] = it["source"] or None
    return templates.TemplateResponse(
        "partials/solo_rounds.html", {"request": request, "items": items})


@router.get("/partials/impulse-overview")
async def impulse_overview_partial(request: Request, profile: str = "default"):
    overview = await impulse_engine.fire_overview(profile)
    return templates.TemplateResponse(
        "partials/impulse_overview.html", {"request": request, "overview": overview})


@router.get("/partials/attributions")
async def attributions_partial(request: Request, profile: str = "default"):
    from ..services.impulse_feedback import recent_attributions
    items = await recent_attributions(profile, limit=15)
    return templates.TemplateResponse(
        "partials/attributions.html", {"request": request, "items": items})


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
    """Receive a sidequest outcome from the plugin's post_llm_call hook. Persist the raw outcome (the log),
    and for INWARD (solo) sidequests run the Phase-3 scorer → a de-rated `solo_round` that folds into mood.
    Outward reach-outs are persist-only here; their emotional effect is Phase-5 reply attribution."""
    import json as _json
    from uuid import uuid4
    body = await request.json()
    profile = str(body.get("profile") or "default")
    session_id = str(body.get("session_id") or "")
    kind = str(body.get("kind") or "")
    response = str(body.get("response") or "")
    # The plugin doesn't send the source capability — bridge it from the fire we stashed at decide time.
    label = str(body.get("label") or "") or await impulse_engine.last_fire_label(profile, kind)
    record = {
        "profile": profile,
        "label": label,
        "kind": kind,
        "response": response,
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

    scored = None
    if kind == "inward" and response.strip():
        from ..services.sidequest_scorer import record_sidequest
        try:
            rec = await record_sidequest(profile, session_id, response)
            if rec:
                scored = {"valence": rec["valence"], "subject": rec["subject"]}
        except Exception as exc:  # scoring must never break outcome ingestion
            logger.warning("sidequest scoring failed: %s", exc)
    return {"ok": True, "stored": True, "scored": scored}
