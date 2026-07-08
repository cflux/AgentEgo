import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import Response, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services.mood_engine import evaluate_mood, get_cached_mood
from ..services.profiles import discover_profiles, resolve_profile

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _get_moods() -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, name, description, color, icon, min_votes FROM moods ORDER BY name"
        )
        return [
            {"id": r[0], "name": r[1], "description": r[2], "color": r[3], "icon": r[4], "min_votes": r[5]}
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()


async def _get_mood(mood_id: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, name, description, color, icon, min_votes FROM moods WHERE id = ?",
            (mood_id,),
        )
        row = await cursor.fetchone()
        return {"id": row[0], "name": row[1], "description": row[2], "color": row[3], "icon": row[4], "min_votes": row[5]} if row else None
    finally:
        await conn.close()


@router.get("/moods")
async def moods_page(request: Request):
    moods = await _get_moods()
    return templates.TemplateResponse("moods.html", {"request": request, "moods": moods})


@router.get("/moods/rules")
async def mood_rules_redirect(profile: str = "default"):
    # The legacy rules page is retired; mood scoring is v2 only. Send old links to the mood page.
    return RedirectResponse(f"/corrective?profile={profile}", status_code=307)


@router.post("/api/moods")
async def create_mood(
    id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#888888"),
    icon: str = Form(""),
    min_votes: int = Form(1),
):
    mood_id = id.strip().lower().replace(" ", "_")
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO moods (id, name, description, color, icon, min_votes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mood_id, name.strip(), description.strip(), color, icon.strip(), max(1, min_votes), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return RedirectResponse("/moods", status_code=303)


@router.get("/api/moods/{mood_id}/edit-form")
async def mood_edit_form(request: Request, mood_id: str):
    mood = await _get_mood(mood_id)
    if not mood:
        return Response(status_code=404)
    return templates.TemplateResponse("partials/mood_edit_row.html", {"request": request, "mood": mood})


@router.post("/api/moods/{mood_id}/edit")
async def update_mood(
    request: Request,
    mood_id: str,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#888888"),
    icon: str = Form(""),
    min_votes: int = Form(1),
):
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE moods SET name=?, description=?, color=?, icon=?, min_votes=? WHERE id=?",
            (name.strip(), description.strip(), color, icon.strip(), max(1, min_votes), mood_id),
        )
        await conn.commit()
    finally:
        await conn.close()
    mood = await _get_mood(mood_id)
    return templates.TemplateResponse("partials/mood_row.html", {"request": request, "mood": mood})


@router.get("/api/moods/{mood_id}/row")
async def mood_row(request: Request, mood_id: str):
    mood = await _get_mood(mood_id)
    if not mood:
        return Response(status_code=404)
    return templates.TemplateResponse("partials/mood_row.html", {"request": request, "mood": mood})


@router.delete("/api/moods/{mood_id}")
async def delete_mood(mood_id: str):
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM mood_thresholds WHERE mood_id = ?", (mood_id,))
        await conn.execute("DELETE FROM mood_defaults WHERE mood_id = ?", (mood_id,))
        await conn.execute("DELETE FROM mood_corrections WHERE target_mood = ?", (mood_id,))
        await conn.execute("DELETE FROM moods WHERE id = ?", (mood_id,))
        await conn.commit()
    finally:
        await conn.close()
    return Response(status_code=200)


# --- Agent-facing mood reads ---

@router.get("/api/mood/current")
async def current_mood_json(profile: str = "default") -> dict:
    """Agent-facing: the profile's current mood + why. Pure read of the cached value — the mood is
    recomputed on a schedule (refresh_all_moods), decoupled from this fetch."""
    mood = await get_cached_mood(profile)
    if not mood:
        return {"profile": profile, "mood": None, "mood_id": None, "vote_count": 0, "why": []}
    return {
        "profile": profile,
        "mood": mood["name"],
        "mood_id": mood["id"],
        "vote_count": mood.get("vote_count", 0),
        "why": mood.get("breakdown") or [],
    }


def render_mood_directive(template: str, name: str, description: str) -> str:
    """Fill the disposition template. Plain replace (not str.format) so stray braces are safe."""
    return (template or "").replace("{mood}", name or "").replace("{description}", description or "")


@router.get("/api/mood/directive", response_class=PlainTextResponse)
async def mood_directive(profile: str = "default"):
    """Agent-facing: the guardrailed disposition block to inject into the system prompt each turn.
    Pure read of the cached mood (recomputed on a schedule, decoupled from this fetch) — cheap to
    hit every turn. Empty when disabled or no mood."""
    from ..services import settings_store
    if (await settings_store.get_setting("mood_directive_enabled", "1")) != "1":
        return PlainTextResponse("")
    mood = await get_cached_mood(profile)
    if not mood:
        return PlainTextResponse("")
    template = await settings_store.get_setting("mood_directive_template", "")
    text = render_mood_directive(template, mood["name"], mood.get("description", ""))
    return PlainTextResponse(text.strip())


# --- Dashboard badge ---

@router.get("/partials/mood-badge")
async def mood_badge_partial(request: Request, profile: str = ""):
    profiles = discover_profiles()
    if profile:
        db_path = resolve_profile(profile)
        mood = await evaluate_mood(profile, db_path=db_path)
        profiles_moods = [{"profile_name": profile, "mood": mood}]
    else:
        profiles_moods = []
        for p in profiles:
            mood = await evaluate_mood(p["name"], db_path=p["db_path"])
            profiles_moods.append({"profile_name": p["name"], "mood": mood})
    multi = len(profiles) > 1
    return templates.TemplateResponse(
        "partials/mood_badge.html",
        {"request": request, "profiles_moods": profiles_moods, "multi": multi},
    )


# --- Mood scoring: corrective-layer view + CRUD (the mood page) ---
from ..services import mood_corrections as _mc


def _parse_emotions_form(form) -> dict:
    """Read the emotion picker: checkboxes named 'emo' + a weight input 'w_<emotion>' each."""
    out = {}
    emos = form.getlist("emo") if hasattr(form, "getlist") else []
    for e in emos:
        try:
            out[e] = round(max(0.0, min(1.0, float(form.get(f"w_{e}", "1.0")))), 2)
        except (TypeError, ValueError):
            out[e] = 1.0
    return out


def _form_list(form, key: str) -> list:
    return [x for x in (form.getlist(key) if hasattr(form, "getlist") else []) if x]


@router.get("/corrective")
async def corrective_page(request: Request, profile: str = "default"):
    view = await _mc.corrective_view(profile, db_path=resolve_profile(profile))
    return templates.TemplateResponse(
        "corrective.html",
        {"request": request, "v": view, "profiles": discover_profiles(), "active_profile": profile},
    )


@router.get("/partials/corrective-panel")
async def corrective_panel(request: Request, profile: str = "default"):
    view = await _mc.corrective_view(profile, db_path=resolve_profile(profile))
    return templates.TemplateResponse(
        "partials/corrective_panel.html", {"request": request, "v": view, "active_profile": profile},
    )


async def _render_moodcfg(request: Request, profile: str):
    rows = await _mc.mood_config_rows(profile, db_path=resolve_profile(profile))
    return templates.TemplateResponse(
        "partials/corrective_moodcfg.html", {"request": request, "cfg_rows": rows, "profile": profile})


@router.post("/api/corrective/resting/toggle")
async def corrective_toggle_resting(request: Request, profile: str = Form(...), mood_id: str = Form(...)):
    conn = await get_ego_db()
    try:
        exists = await (await conn.execute(
            "SELECT 1 FROM mood_defaults WHERE profile_name=? AND mood_id=?", (profile, mood_id))).fetchone()
        if exists:
            await conn.execute("DELETE FROM mood_defaults WHERE profile_name=? AND mood_id=?", (profile, mood_id))
        else:
            await conn.execute("INSERT OR IGNORE INTO mood_defaults (profile_name, mood_id) VALUES (?, ?)",
                               (profile, mood_id))
        await conn.commit()
    finally:
        await conn.close()
    return await _render_moodcfg(request, profile)


@router.post("/api/corrective/threshold")
async def corrective_set_threshold(request: Request, profile: str = Form(...), mood_id: str = Form(...),
                                   min_votes: int = Form(...)):
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO mood_thresholds (profile_name, mood_id, min_votes) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_name, mood_id) DO UPDATE SET min_votes = excluded.min_votes",
            (profile, mood_id, max(1, min_votes)))
        await conn.commit()
    finally:
        await conn.close()
    return await _render_moodcfg(request, profile)


@router.post("/api/corrections")
async def create_correction_ep(request: Request):
    form = await request.form()
    profile = str(form.get("profile", "default"))
    target = str(form.get("target_mood", "")).strip()
    emos = _parse_emotions_form(form)
    if target and emos:
        await _mc.create_correction(
            profile, target, emos,
            relation=str(form.get("relation", "none")),
            mode=_form_list(form, "mode"),
            strength=float(form.get("strength", 0.6) or 0.6),
            note=str(form.get("note", "")).strip())
    return RedirectResponse(f"/corrective?profile={profile}", status_code=303)


@router.post("/api/corrections/{cid}/edit")
async def edit_correction_ep(request: Request, cid: str):
    form = await request.form()
    profile = str(form.get("profile", "default"))
    await _mc.update_correction(
        cid,
        agent_emotions=_parse_emotions_form(form),
        relation=str(form.get("relation", "none")),
        mode=_form_list(form, "mode"),
        strength=float(form.get("strength", 0.6) or 0.6),
        note=str(form.get("note", "")).strip())
    return RedirectResponse(f"/corrective?profile={profile}", status_code=303)


@router.patch("/api/corrections/{cid}/toggle")
async def toggle_correction_ep(cid: str):
    await _mc.toggle_correction(cid)
    return Response(status_code=200)


@router.delete("/api/corrections/{cid}")
async def delete_correction_ep(cid: str):
    await _mc.delete_correction(cid)
    return Response(status_code=200)


# --- Mood exit triggers (directed abrupt transitions) CRUD ---
from ..services import mood_exits as _mx


def _parse_exit(form) -> dict:
    ctype = "signal" if str(form.get("condition_type", "llm")) == "signal" else "llm"
    if ctype == "signal":
        try:
            val = float(form.get("sig_value", 0) or 0)
        except (TypeError, ValueError):
            val = 0.0
        condition = {"metric": str(form.get("sig_metric", "")).strip(),
                     "op": str(form.get("sig_op", ">")).strip(), "value": val}
    else:
        condition = {"text": str(form.get("condition_text", "")).strip()}
    return {
        "source_mood": str(form.get("source_mood", "")).strip() or None,
        "target_mood": str(form.get("target_mood", "")).strip(),
        "condition_type": ctype, "condition": condition,
        "hard": bool(form.get("hard")), "note": str(form.get("note", "")).strip(),
    }


@router.post("/api/exits")
async def create_exit_ep(request: Request):
    form = await request.form()
    profile = str(form.get("profile", "default"))
    d = _parse_exit(form)
    ok = d["target_mood"] and (d["condition"].get("text") or d["condition"].get("metric"))
    if ok:
        await _mx.create_exit(profile, d["target_mood"], d["condition_type"], d["condition"],
                              source_mood=d["source_mood"], hard=d["hard"], note=d["note"])
    return RedirectResponse(f"/corrective?profile={profile}", status_code=303)


@router.post("/api/exits/{eid}/edit")
async def edit_exit_ep(request: Request, eid: str):
    form = await request.form()
    profile = str(form.get("profile", "default"))
    d = _parse_exit(form)
    await _mx.update_exit(eid, source_mood=d["source_mood"], target_mood=d["target_mood"],
                          condition_type=d["condition_type"], condition=d["condition"],
                          hard=d["hard"], note=d["note"])
    return RedirectResponse(f"/corrective?profile={profile}", status_code=303)


@router.patch("/api/exits/{eid}/toggle")
async def toggle_exit_ep(eid: str):
    await _mx.toggle_exit(eid)
    return Response(status_code=200)


@router.delete("/api/exits/{eid}")
async def delete_exit_ep(eid: str):
    await _mx.delete_exit(eid)
    return Response(status_code=200)
