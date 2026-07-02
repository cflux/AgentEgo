from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..services import settings_store
from ..services.llm_client import ping
from ..db.ego import get_ego_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Fields the worker needs to construct LLM calls (consumed via /config/model).
_MODEL_FIELDS = ["llm_backend", "llm_base_url", "llm_model", "llm_temperature"]


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


# GoEmotions labels, grouped for the checkbox UI.
EMOTION_GROUPS = {
    "Positive": ["admiration", "amusement", "approval", "caring", "desire", "excitement",
                 "gratitude", "joy", "love", "optimism", "pride", "relief"],
    "Negative": ["anger", "annoyance", "disappointment", "disapproval", "disgust",
                 "embarrassment", "fear", "grief", "nervousness", "remorse", "sadness"],
    "Other": ["confusion", "curiosity", "realization", "surprise", "neutral"],
}


@router.get("/config")
async def config_page(request: Request):
    settings = await settings_store.get_all_settings()
    low_signal = await settings_store.get_low_signal_emotions()
    taxonomy = await settings_store.get_emotion_taxonomy()
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT id, name, icon FROM moods ORDER BY name")
        moods = [{"id": r[0], "name": r[1], "icon": r[2] or ""} for r in await cursor.fetchall()]
    finally:
        await conn.close()
    return templates.TemplateResponse(
        "model_config.html",
        {"request": request, "settings": settings, "masked_key": _mask_key(settings.get("llm_api_key", "")),
         "taxonomy": taxonomy, "low_signal": low_signal, "moods": moods},
    )


@router.get("/config/model")
async def get_model_config() -> dict:
    """Worker pulls its LLM connection config from here (no hardcoded model)."""
    cfg = await settings_store.get_llm_config()
    # API key intentionally included so the worker can authenticate.
    return cfg


@router.post("/config/model")
async def update_model_config(
    request: Request,
    llm_backend: str = Form("deepseek"),
    llm_base_url: str = Form(""),
    llm_model: str = Form(""),
    llm_temperature: str = Form("0.7"),
    llm_api_key: str = Form(""),
    evolution_alpha: str = Form("0.2"),
    seed_deviation_band: str = Form("0.35"),
    trait_drift_delta: str = Form("0.1"),
    round_exchanges: str = Form("3"),
    mood_lookback_rounds: str = Form("20"),
    scoring_backend: str = Form("llm"),
    emotion_taxonomy: str = Form(""),
    sentiment_llm_url: str = Form("http://localhost:11434"),
    sentiment_llm_model: str = Form(""),
    llm_mood_threshold: str = Form("6"),
    llm_mood_weight: str = Form("1"),
    mood_inertia_bonus: str = Form("2"),
    mood_jump_penalty: str = Form("3"),
    mood_adjacency: str = Form(""),
    mood_cascade: str = Form(""),
    mood_decay_grace: str = Form("5"),
    mood_decay_rate: str = Form("3"),
    mood_decay_cooldown: str = Form("4"),
    mood_directive_template: str = Form(""),
    mood_directive_file: str = Form(""),
):
    # Low-signal emotions come from checkboxes (zero or more 'low_signal' values).
    form = await request.form()
    emos = ",".join(e.strip().lower() for e in form.getlist("low_signal") if e.strip())
    # Normalize the taxonomy textarea (newlines/commas) to a comma list.
    taxonomy = ",".join(
        e.strip().lower() for e in emotion_taxonomy.replace("\n", ",").split(",") if e.strip()
    )
    updates = {
        "llm_backend": llm_backend.strip(),
        "llm_base_url": llm_base_url.strip(),
        "llm_model": llm_model.strip(),
        "llm_temperature": llm_temperature.strip(),
        "evolution_alpha": evolution_alpha.strip(),
        "seed_deviation_band": seed_deviation_band.strip(),
        "trait_drift_delta": trait_drift_delta.strip(),
        "low_signal_emotions": emos,
        "round_exchanges": round_exchanges.strip(),
        "mood_lookback_rounds": mood_lookback_rounds.strip(),
        "scoring_backend": scoring_backend.strip(),
        "emotion_taxonomy": taxonomy,
        "sentiment_llm_url": sentiment_llm_url.strip(),
        "sentiment_llm_model": sentiment_llm_model.strip(),
        "llm_mood_votes_enabled": "1" if form.get("llm_mood_votes_enabled") else "0",
        "llm_mood_threshold": llm_mood_threshold.strip(),
        "llm_mood_weight": llm_mood_weight.strip(),
        "mood_transitions_enabled": "1" if form.get("mood_transitions_enabled") else "0",
        "mood_inertia_bonus": mood_inertia_bonus.strip(),
        "mood_jump_penalty": mood_jump_penalty.strip(),
        "mood_cascade_enabled": "1" if form.get("mood_cascade_enabled") else "0",
        "mood_decay_enabled": "1" if form.get("mood_decay_enabled") else "0",
        "mood_decay_grace": mood_decay_grace.strip(),
        "mood_decay_rate": mood_decay_rate.strip(),
        "mood_decay_cooldown": mood_decay_cooldown.strip(),
        "mood_directive_enabled": "1" if form.get("mood_directive_enabled") else "0",
        "mood_directive_template": mood_directive_template,
        "mood_directive_file": mood_directive_file.strip(),
    }
    # Only overwrite JSON graph/cascade if valid JSON was submitted (avoid clobbering with junk).
    import json as _json
    for _field, _val in (("mood_adjacency", mood_adjacency), ("mood_cascade", mood_cascade)):
        if _val.strip():
            try:
                _json.loads(_val)
                updates[_field] = _val.strip()
            except ValueError:
                pass
    # Only overwrite the API key when a new value is submitted (blank = keep existing).
    if llm_api_key.strip():
        updates["llm_api_key"] = llm_api_key.strip()
    await settings_store.set_settings(updates)

    settings = await settings_store.get_all_settings()
    return templates.TemplateResponse(
        "partials/config_saved.html",
        {"request": request, "settings": settings, "masked_key": _mask_key(settings.get("llm_api_key", ""))},
    )


@router.post("/config/test")
async def test_connection(request: Request):
    result = await ping()
    return templates.TemplateResponse(
        "partials/config_test.html",
        {"request": request, "result": result},
    )
