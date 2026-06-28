import json
import time
from uuid import uuid4
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services.mood_engine import evaluate_mood
from ..services.profiles import discover_profiles, resolve_profile

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

VALID_RULE_TYPES = {
    "mode_streak", "mode_count",
    "sentiment_user", "sentiment_agent", "sentiment_mismatch",
    "topic_keyword",
}
VALID_MODES = {"work", "social", "informative", "serious", "flirting", "creative", "support"}


# --- Helpers ---

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


async def _get_rules(profile_name: str) -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, mood_id, rule_type, params, label, enabled, mood_gate FROM mood_rules "
            "WHERE profile_name = ? ORDER BY created_at ASC",
            (profile_name,),
        )
        return [
            {
                "id": r[0], "mood_id": r[1], "rule_type": r[2],
                "params": json.loads(r[3]), "label": r[4],
                "enabled": bool(r[5]), "mood_gate": r[6],
            }
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()


async def _get_thresholds(profile_name: str) -> dict:
    """Returns {mood_id: min_votes} for per-profile overrides."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT mood_id, min_votes FROM mood_thresholds WHERE profile_name = ?",
            (profile_name,),
        )
        return {r[0]: r[1] for r in await cursor.fetchall()}
    finally:
        await conn.close()


def _parse_params(rule_type: str, form) -> dict:
    if rule_type == "mode_streak":
        return {
            "mode": str(form.get("params_mode", "work")),
            "count": max(1, int(form.get("params_count", 3))),
            "negate": form.get("params_negate") == "1",
        }
    elif rule_type == "mode_count":
        return {
            "mode": str(form.get("params_mode", "work")),
            "min_count": max(1, int(form.get("params_min_count", 2))),
            "lookback": max(1, int(form.get("params_lookback", 5))),
            "negate": form.get("params_negate") == "1",
        }
    elif rule_type in ("sentiment_user", "sentiment_agent"):
        raw = form.getlist("params_emotions") if hasattr(form, "getlist") else []
        if isinstance(raw, str):
            raw = [raw]
        return {
            "emotions": [e for e in raw if e],
            "lookback": max(1, int(form.get("params_lookback", 1))),
            "min_count": max(1, int(form.get("params_min_count", 1))),
        }
    elif rule_type == "sentiment_mismatch":
        raw = form.getlist("params_emotions") if hasattr(form, "getlist") else []
        if isinstance(raw, str):
            raw = [raw]
        return {
            "emotions": [e for e in raw if e],
            "direction": str(form.get("params_direction", "either")),
            "lookback": max(1, int(form.get("params_lookback", 1))),
            "min_count": max(1, int(form.get("params_min_count", 1))),
        }
    elif rule_type == "topic_keyword":
        raw = str(form.get("params_keywords", ""))
        keywords = [k.strip() for k in raw.split(",") if k.strip()]
        return {
            "keywords": keywords,
            "lookback": max(1, int(form.get("params_lookback", 5))),
            "min_count": max(1, int(form.get("params_min_count", 1))),
        }
    return {}


def _rule_summary(rule: dict) -> str:
    p = rule["params"]
    rt = rule["rule_type"]
    if rt == "mode_streak":
        op = "not in" if p.get("negate") else "all in"
        return f"Last {p.get('count', 3)} sessions {op} <strong>{p.get('mode', '?')}</strong> mode"
    elif rt == "mode_count":
        op = "not in" if p.get("negate") else "in"
        return f"{p.get('min_count', 2)}+ of last {p.get('lookback', 5)} sessions {op} <strong>{p.get('mode', '?')}</strong> mode"
    elif rt in ("sentiment_user", "sentiment_agent"):
        who = "User" if rt == "sentiment_user" else "Agent"
        emo = ", ".join(p.get("emotions", [])[:4]) or "—"
        return f"{who} felt <strong>{emo}</strong> in {p.get('min_count', 1)}+ of last {p.get('lookback', 1)}"
    elif rt == "sentiment_mismatch":
        emo = ", ".join(p.get("emotions", [])[:4]) or "—"
        dir_labels = {
            "user_only": "user felt / agent didn't",
            "agent_only": "agent felt / user didn't",
            "either": "either direction",
        }
        dir_str = dir_labels.get(p.get("direction", "either"), "either direction")
        return f"Mismatch <strong>{emo}</strong> ({dir_str}) in {p.get('min_count', 1)}+ of last {p.get('lookback', 1)}"
    elif rt == "topic_keyword":
        kw = ", ".join(f"'{k}'" for k in p.get("keywords", [])[:4]) or "—"
        return f"Topic contains {kw} in {p.get('min_count', 1)}+ of last {p.get('lookback', 5)}"
    return rt


# --- Page routes ---

@router.get("/moods")
async def moods_page(request: Request):
    moods = await _get_moods()
    return templates.TemplateResponse("moods.html", {"request": request, "moods": moods})


@router.get("/moods/rules")
async def mood_rules_page(request: Request, profile: str = "default"):
    moods = await _get_moods()
    rules = await _get_rules(profile)
    for r in rules:
        r["summary"] = _rule_summary(r)
    profiles = discover_profiles()
    db_path = resolve_profile(profile)
    current_mood = await evaluate_mood(profile, db_path=db_path)
    thresholds = await _get_thresholds(profile)
    return templates.TemplateResponse(
        "mood_rules.html",
        {
            "request": request,
            "moods": moods,
            "rules": rules,
            "profiles": profiles,
            "active_profile": profile,
            "current_mood": current_mood,
            "thresholds": thresholds,
        },
    )


# --- Canonical mood CRUD ---

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
        await conn.execute("DELETE FROM mood_rules WHERE mood_id = ?", (mood_id,))
        await conn.execute("DELETE FROM mood_thresholds WHERE mood_id = ?", (mood_id,))
        await conn.execute("DELETE FROM moods WHERE id = ?", (mood_id,))
        await conn.commit()
    finally:
        await conn.close()
    return Response(status_code=200)


# --- Rule CRUD ---

@router.get("/api/mood/rule-params")
async def rule_params_partial(request: Request, rule_type: str = ""):
    if rule_type not in VALID_RULE_TYPES:
        return HTMLResponse('<p style="color:var(--pico-muted-color); font-size:0.85rem;">Select a rule type above.</p>')
    return templates.TemplateResponse(f"partials/rule_params/{rule_type}.html", {"request": request})


@router.post("/api/mood/rules")
async def create_rule(request: Request):
    form = await request.form()
    profile_name = str(form.get("profile_name", "default"))
    mood_id = str(form.get("mood_id", ""))
    rule_type = str(form.get("rule_type", ""))
    label = str(form.get("label", "")).strip() or None
    mood_gate = str(form.get("mood_gate", "")).strip() or None

    if rule_type not in VALID_RULE_TYPES:
        return RedirectResponse(f"/moods/rules?profile={profile_name}", status_code=303)

    params = _parse_params(rule_type, form)

    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO mood_rules (id, profile_name, mood_id, rule_type, params, label, mood_gate, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (str(uuid4()), profile_name, mood_id, rule_type, json.dumps(params), label, mood_gate, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()

    return RedirectResponse(f"/moods/rules?profile={profile_name}", status_code=303)


@router.delete("/api/mood/rules/{rule_id}")
async def delete_rule(rule_id: str):
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM mood_rules WHERE id = ?", (rule_id,))
        await conn.commit()
    finally:
        await conn.close()
    return Response(status_code=200)


@router.patch("/api/mood/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: str):
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE mood_rules SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",
            (rule_id,),
        )
        await conn.commit()
    finally:
        await conn.close()
    return Response(status_code=200)


# --- Threshold CRUD ---

@router.post("/api/mood/thresholds")
async def set_threshold(request: Request, profile_name: str = Form(...), mood_id: str = Form(...), min_votes: int = Form(...)):
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO mood_thresholds (profile_name, mood_id, min_votes) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_name, mood_id) DO UPDATE SET min_votes = excluded.min_votes",
            (profile_name, mood_id, max(1, min_votes)),
        )
        await conn.commit()
    finally:
        await conn.close()
    mood = await _get_mood(mood_id)
    thresholds = await _get_thresholds(profile_name)
    return templates.TemplateResponse(
        "partials/threshold_row.html",
        {"request": request, "mood": mood, "profile_name": profile_name, "thresholds": thresholds},
    )


@router.delete("/api/mood/thresholds/{profile_name}/{mood_id}")
async def clear_threshold(request: Request, profile_name: str, mood_id: str):
    conn = await get_ego_db()
    try:
        await conn.execute(
            "DELETE FROM mood_thresholds WHERE profile_name=? AND mood_id=?",
            (profile_name, mood_id),
        )
        await conn.commit()
    finally:
        await conn.close()
    mood = await _get_mood(mood_id)
    thresholds = await _get_thresholds(profile_name)
    return templates.TemplateResponse(
        "partials/threshold_row.html",
        {"request": request, "mood": mood, "profile_name": profile_name, "thresholds": thresholds},
    )


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
