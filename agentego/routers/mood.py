import json
import time
from uuid import uuid4
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, Response, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db.ego import get_ego_db
from ..services.mood_engine import evaluate_mood, explain_mood
from ..services.profiles import discover_profiles, resolve_profile
from ..services.llm_client import chat, LLMError
from ..services.affinity_engine import get_traits

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

VALID_RULE_TYPES = {
    "mode_streak", "mode_count",
    "sentiment_user", "sentiment_agent", "sentiment_mismatch",
    "topic_keyword", "prev_mood",
}
VALID_MODES = {"work", "social", "informative", "serious", "flirting", "creative", "support"}
VALID_EMOTIONS = {
    "admiration", "amusement", "anger", "annoyance", "approval", "caring", "confusion",
    "curiosity", "desire", "disappointment", "disapproval", "disgust", "embarrassment",
    "excitement", "fear", "gratitude", "grief", "joy", "love", "nervousness", "neutral",
    "optimism", "pride", "realization", "relief", "remorse", "sadness", "surprise",
}


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


async def _get_mood_history(profile_name: str, limit: int = 30) -> list:
    """Recent mood *changes* for a profile, newest first."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT prev_mood_id, mood_id, vote_count, changed_at FROM mood_history "
            "WHERE profile_name = ? ORDER BY changed_at DESC LIMIT ?",
            (profile_name, limit),
        )
        return [{"prev": r[0], "mood": r[1], "votes": r[2], "at": r[3]}
                for r in await cursor.fetchall()]
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


async def _get_defaults(profile_name: str) -> set:
    """Mood ids in this profile's resting-mood set."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT mood_id FROM mood_defaults WHERE profile_name = ?", (profile_name,)
        )
        return {r[0] for r in await cursor.fetchall()}
    finally:
        await conn.close()


def _parse_params(rule_type: str, form) -> dict:
    if rule_type == "prev_mood":
        raw = form.getlist("params_moods") if hasattr(form, "getlist") else []
        if isinstance(raw, str):
            raw = [raw]
        return {"moods": [m for m in raw if m], "negate": form.get("params_negate") == "1"}
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
    if rt == "prev_mood":
        op = "is not" if p.get("negate") else "is"
        ms = ", ".join(f"<strong>{m}</strong>" for m in p.get("moods", [])[:4]) or "—"
        return f"Previous mood {op} {ms}"
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


def _sort_and_group(rules: list, moods: list) -> list:
    """Group rules by target mood (sorted by mood name), flagging each group's first
    row so the table can draw a separator. Stable sort keeps creation order within a mood."""
    name_by_id = {m["id"]: m["name"] for m in moods}
    rules.sort(key=lambda r: name_by_id.get(r["mood_id"], r["mood_id"]).lower())
    prev = None
    for i, r in enumerate(rules):
        r["is_group_start"] = (r["mood_id"] != prev) and i > 0
        prev = r["mood_id"]
    return rules


@router.get("/moods/rules")
async def mood_rules_page(request: Request, profile: str = "default"):
    moods = await _get_moods()
    rules = await _get_rules(profile)
    for r in rules:
        r["summary"] = _rule_summary(r)
    _sort_and_group(rules, moods)
    profiles = discover_profiles()
    db_path = resolve_profile(profile)
    current_mood = await evaluate_mood(profile, db_path=db_path)
    thresholds = await _get_thresholds(profile)
    defaults = await _get_defaults(profile)
    moods_by_id = {m["id"]: m for m in moods}
    history = await _get_mood_history(profile)
    for h in history:
        h["from"] = moods_by_id.get(h["prev"])
        h["to"] = moods_by_id.get(h["mood"])
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
            "defaults": defaults,
            "history": history,
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
        await conn.execute("DELETE FROM mood_defaults WHERE mood_id = ?", (mood_id,))
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
    moods = await _get_moods()  # needed by the prev_mood selector
    return templates.TemplateResponse(
        f"partials/rule_params/{rule_type}.html", {"request": request, "moods": moods}
    )


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

    # Skip if an identical rule (same type + mood + params) already exists.
    existing = await _get_rules(profile_name)
    if _rule_dupe_key(rule_type, mood_id, params) in {
        _rule_dupe_key(r["rule_type"], r["mood_id"], r["params"]) for r in existing
    }:
        return RedirectResponse(f"/moods/rules?profile={profile_name}", status_code=303)

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


async def _get_rule(rule_id: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, profile_name, mood_id, rule_type, params, label, enabled, mood_gate "
            "FROM mood_rules WHERE id = ?",
            (rule_id,),
        )
        r = await cursor.fetchone()
    finally:
        await conn.close()
    if not r:
        return None
    return {
        "id": r[0], "profile_name": r[1], "mood_id": r[2], "rule_type": r[3],
        "params": json.loads(r[4]), "label": r[5], "enabled": bool(r[6]), "mood_gate": r[7],
    }


@router.get("/api/mood/rules/{rule_id}/edit-form")
async def rule_edit_form(request: Request, rule_id: str):
    rule = await _get_rule(rule_id)
    if not rule:
        return Response(status_code=404)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/mood_rule_edit_row.html", {"request": request, "rule": rule, "moods": moods}
    )


@router.get("/api/mood/rules/{rule_id}/row")
async def rule_row(request: Request, rule_id: str):
    rule = await _get_rule(rule_id)
    if not rule:
        return Response(status_code=404)
    rule["summary"] = _rule_summary(rule)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/mood_rule_row.html", {"request": request, "rule": rule, "moods": moods}
    )


@router.post("/api/mood/rules/{rule_id}/edit")
async def edit_rule(request: Request, rule_id: str):
    rule = await _get_rule(rule_id)
    if not rule:
        return Response(status_code=404)
    form = await request.form()
    mood_id = str(form.get("mood_id", rule["mood_id"]))
    mood_gate = str(form.get("mood_gate", "")).strip() or None
    label = str(form.get("label", "")).strip() or None
    params = _parse_params(rule["rule_type"], form)  # rule_type is fixed on edit
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE mood_rules SET mood_id=?, params=?, label=?, mood_gate=? WHERE id=?",
            (mood_id, json.dumps(params), label, mood_gate, rule_id),
        )
        await conn.commit()
    finally:
        await conn.close()
    updated = await _get_rule(rule_id)
    updated["summary"] = _rule_summary(updated)
    moods = await _get_moods()
    return templates.TemplateResponse(
        "partials/mood_rule_row.html", {"request": request, "rule": updated, "moods": moods}
    )


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


# --- Default mood set CRUD ---

@router.post("/api/mood/defaults/toggle")
async def toggle_default(request: Request, profile_name: str = Form(...), mood_id: str = Form(...)):
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM mood_defaults WHERE profile_name=? AND mood_id=?", (profile_name, mood_id)
        )
        exists = await cursor.fetchone()
        if exists:
            await conn.execute(
                "DELETE FROM mood_defaults WHERE profile_name=? AND mood_id=?", (profile_name, mood_id)
            )
        else:
            await conn.execute(
                "INSERT OR IGNORE INTO mood_defaults (profile_name, mood_id) VALUES (?, ?)",
                (profile_name, mood_id),
            )
        await conn.commit()
    finally:
        await conn.close()
    mood = await _get_mood(mood_id)
    defaults = await _get_defaults(profile_name)
    return templates.TemplateResponse(
        "partials/default_row.html",
        {"request": request, "mood": mood, "profile_name": profile_name, "defaults": defaults},
    )


# --- Dashboard badge ---

@router.get("/api/mood/current")
async def current_mood_json(profile: str = "default") -> dict:
    """Agent-facing: the profile's current mood + why it's in that mood."""
    mood = await evaluate_mood(profile, db_path=resolve_profile(profile))
    if not mood:
        return {"profile": profile, "mood": None, "mood_id": None, "vote_count": 0, "why": []}
    return {
        "profile": profile,
        "mood": mood["name"],
        "mood_id": mood["id"],
        "vote_count": mood.get("vote_count", 0),
        "why": mood.get("breakdown") or [],
    }


# --- Debug computation ---

@router.get("/partials/mood-debug")
async def mood_debug_partial(request: Request, profile: str = "default"):
    data = await explain_mood(profile, db_path=resolve_profile(profile))
    return templates.TemplateResponse(
        "partials/mood_debug.html", {"request": request, "d": data, "profile": profile}
    )


# --- Natural-language rule builder (LLM) ---

_RULE_BUILDER_HEAD = (
    "You convert a plain-English description of an agent 'mood rule' into ONE structured JSON "
    "rule. When a rule's condition is met it casts a single vote for a mood.\n\n"
    "Available moods (use the id on the left):\n"
)
_RULE_BUILDER_TAIL = (
    "\n\nChoose exactly ONE rule_type and fill its params:\n"
    '- mode_streak: {"mode": M, "count": N, "negate": bool} — the last N conversations were ALL in mode M (or all NOT in, if negate).\n'
    '- mode_count: {"mode": M, "min_count": K, "lookback": N, "negate": bool} — at least K of the last N conversations were in mode M.\n'
    '- sentiment_user: {"emotions": [...], "lookback": N, "min_count": K} — the USER felt one of these emotions in K+ of the last N.\n'
    '- sentiment_agent: {"emotions": [...], "lookback": N, "min_count": K} — the AGENT expressed one of these emotions in K+ of the last N.\n'
    '- sentiment_mismatch: {"emotions": [...], "direction": "either"|"user_only"|"agent_only", "lookback": N, "min_count": K} — emotion present for one party but not the other.\n'
    '- topic_keyword: {"keywords": [...], "lookback": N, "min_count": K} — the conversation topic contained a keyword.\n'
    '- prev_mood: {"moods": [<mood_id>...], "negate": bool} — the mood from the PREVIOUS evaluation is (or is not) one of these mood ids. Good for momentum/transitions.\n\n'
    "Valid modes: work, social, informative, serious, flirting, creative, support.\n"
    "Valid emotions: admiration, amusement, anger, annoyance, approval, caring, confusion, "
    "curiosity, desire, disappointment, disapproval, disgust, embarrassment, excitement, fear, "
    "gratitude, grief, joy, love, nervousness, neutral, optimism, pride, realization, relief, "
    "remorse, sadness, surprise.\n\n"
    "Map synonyms (flirty/romantic->flirting, coding/technical->work, helping->support, etc.)."
)
_RULE_BUILDER_OUTPUT = (
    '\n\nOutput a JSON object ONLY: {"rules": [{"mood_id": "...", "rule_type": "...", '
    '"params": {...}, "label": "<short human summary>"}, ...]}.\n'
    "If the description contains multiple distinct conditions (often joined by 'and'/'also'/'plus'), "
    "output a SEPARATE rule for EACH condition. Do NOT output a rule that duplicates one the agent "
    "already has — omit it from the array."
)


def _canon_params(rule_type: str, params: dict) -> str:
    """Canonical string for duplicate detection (order-independent for lists)."""
    p = dict(params or {})
    for k in ("emotions", "keywords", "moods"):
        if isinstance(p.get(k), list):
            p[k] = sorted(str(x).lower() for x in p[k])
    return json.dumps(p, sort_keys=True)


def _rule_dupe_key(rule_type: str, mood_id: str, params: dict) -> tuple:
    return (rule_type, mood_id, _canon_params(rule_type, params))


def _clean_llm_params(rule_type: str, params: dict | None) -> dict:
    p = params or {}

    def _i(k, d):
        try:
            return max(1, int(p.get(k, d)))
        except (TypeError, ValueError):
            return d

    def _mode(k, d="work"):
        m = str(p.get(k, d)).strip().lower()
        return m if m in VALID_MODES else d

    def _emos():
        raw = p.get("emotions", [])
        if isinstance(raw, str):
            raw = [raw]
        return [str(e).strip().lower() for e in raw if str(e).strip().lower() in VALID_EMOTIONS]

    if rule_type == "prev_mood":
        raw = p.get("moods", [])
        if isinstance(raw, str):
            raw = [raw]
        return {"moods": [str(m).strip() for m in raw if str(m).strip()], "negate": bool(p.get("negate", False))}
    if rule_type == "mode_streak":
        return {"mode": _mode("mode"), "count": _i("count", 3), "negate": bool(p.get("negate", False))}
    if rule_type == "mode_count":
        return {"mode": _mode("mode"), "min_count": _i("min_count", 2),
                "lookback": _i("lookback", 5), "negate": bool(p.get("negate", False))}
    if rule_type in ("sentiment_user", "sentiment_agent"):
        return {"emotions": _emos(), "lookback": _i("lookback", 1), "min_count": _i("min_count", 1)}
    if rule_type == "sentiment_mismatch":
        d = str(p.get("direction", "either")).strip().lower()
        if d not in ("either", "user_only", "agent_only"):
            d = "either"
        return {"emotions": _emos(), "direction": d, "lookback": _i("lookback", 1), "min_count": _i("min_count", 1)}
    if rule_type == "topic_keyword":
        raw = p.get("keywords", [])
        if isinstance(raw, str):
            raw = raw.split(",")
        kws = [str(k).strip() for k in raw if str(k).strip()]
        return {"keywords": kws, "lookback": _i("lookback", 5), "min_count": _i("min_count", 1)}
    return {}


def _err(msg: str) -> HTMLResponse:
    return HTMLResponse(f'<p style="color:#e57373; margin:0; font-size:0.85rem;">⚠ {msg}</p>')


@router.post("/api/mood/rules/from-text")
async def create_rule_from_text(request: Request, profile_name: str = Form(...), description: str = Form(...)):
    description = description.strip()
    if not description:
        return _err("Describe a rule first.")
    moods = await _get_moods()
    mood_ids = {m["id"] for m in moods}
    system = _RULE_BUILDER_HEAD + "\n".join(f"- {m['id']}: {m['name']}" for m in moods) + _RULE_BUILDER_TAIL

    # Give the LLM the agent's personality so it picks moods/conditions that fit the character.
    traits = await get_traits(profile_name)
    if traits and traits.get("current"):
        cur = traits["current"]
        bits = []
        if cur.get("summary"):
            bits.append(cur["summary"])
        if cur.get("values"):
            bits.append("Core values: " + ", ".join(cur["values"]) + ".")
        if bits:
            system += "\n\nThis agent's personality (build rules that fit this character):\n" + " ".join(bits)

    # Give the LLM the agent's existing rules so it avoids duplicates and complements them.
    existing = await _get_rules(profile_name)
    if existing:
        lines = []
        for r in existing:
            state = "" if r.get("enabled", True) else " [disabled]"
            summary = _rule_summary(r).replace("<strong>", "").replace("</strong>", "")
            lines.append(f"- votes for {r['mood_id']}: {summary}{state}")
        system += (
            "\n\nThis agent ALREADY has these rules — do NOT create a duplicate; "
            "prefer a rule that complements them:\n" + "\n".join(lines)
        )
    system += _RULE_BUILDER_OUTPUT

    try:
        raw = await chat(
            [{"role": "system", "content": system}, {"role": "user", "content": description}],
            response_json=True, max_tokens=1500,
        )
        data = json.loads(raw)
    except LLMError as e:
        return _err(f"LLM error: {e}")
    except json.JSONDecodeError:
        return _err("Couldn't parse a rule from that — try rephrasing more concretely.")

    # The model may return one rule or several.
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        proposed = data["rules"]
    elif isinstance(data, list):
        proposed = data
    elif isinstance(data, dict) and data.get("rule_type"):
        proposed = [data]
    else:
        proposed = []
    if not proposed:
        return templates.TemplateResponse(
            "partials/nl_rule_result.html",
            {"request": request, "added": [],
             "skipped": ["nothing to add — it may already exist, or try describing the condition more concretely"]},
        )

    # Existing rules → duplicate keys.
    seen = {_rule_dupe_key(r["rule_type"], r["mood_id"], r["params"]) for r in existing}

    added, skipped = [], []
    for item in proposed[:8]:
        if not isinstance(item, dict):
            continue
        rule_type = str(item.get("rule_type", "")).strip()
        mood_id = str(item.get("mood_id", "")).strip()
        label = str(item.get("label", "")).strip() or None
        if rule_type not in VALID_RULE_TYPES:
            skipped.append(f"unrecognized rule type ({rule_type or '—'})")
            continue
        if mood_id not in mood_ids:
            skipped.append(f"mood '{mood_id or '—'}' doesn't exist")
            continue
        params = _clean_llm_params(rule_type, item.get("params"))
        if rule_type in ("sentiment_user", "sentiment_agent", "sentiment_mismatch") and not params.get("emotions"):
            skipped.append(f"{label or rule_type}: no valid emotions")
            continue
        if rule_type == "topic_keyword" and not params.get("keywords"):
            skipped.append(f"{label or rule_type}: no keywords")
            continue
        if rule_type == "prev_mood":
            params["moods"] = [m for m in params.get("moods", []) if m in mood_ids]
            if not params["moods"]:
                skipped.append(f"{label or rule_type}: no valid previous moods")
                continue
        key = _rule_dupe_key(rule_type, mood_id, params)
        if key in seen:
            skipped.append(f"{label or rule_type} — already exists (duplicate)")
            continue
        seen.add(key)
        conn = await get_ego_db()
        try:
            await conn.execute(
                "INSERT INTO mood_rules (id, profile_name, mood_id, rule_type, params, label, mood_gate, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (str(uuid4()), profile_name, mood_id, rule_type, json.dumps(params), label, None, time.time()),
            )
            await conn.commit()
        finally:
            await conn.close()
        summary = _rule_summary({"rule_type": rule_type, "params": params}).replace("<strong>", "").replace("</strong>", "")
        added.append(f"{label + ' — ' if label else ''}{summary} → {mood_id}")

    headers = {"HX-Trigger": "rulesUpdated"} if added else {}
    return templates.TemplateResponse(
        "partials/nl_rule_result.html",
        {"request": request, "added": added, "skipped": skipped},
        headers=headers,
    )


@router.get("/partials/mood-rules-list")
async def mood_rules_list_partial(request: Request, profile: str = "default"):
    moods = await _get_moods()
    rules = await _get_rules(profile)
    for r in rules:
        r["summary"] = _rule_summary(r)
    _sort_and_group(rules, moods)
    return templates.TemplateResponse(
        "partials/mood_rules_list.html",
        {"request": request, "rules": rules, "moods": moods, "active_profile": profile},
    )


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
