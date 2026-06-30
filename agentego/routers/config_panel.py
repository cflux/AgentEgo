from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..services import settings_store
from ..services.llm_client import ping

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


@router.get("/config")
async def config_page(request: Request):
    settings = await settings_store.get_all_settings()
    return templates.TemplateResponse(
        "model_config.html",
        {"request": request, "settings": settings, "masked_key": _mask_key(settings.get("llm_api_key", ""))},
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
    low_signal_emotions: str = Form("neutral,approval"),
):
    # Normalize the emotion list to clean comma-separated lowercase.
    emos = ",".join(e.strip().lower() for e in low_signal_emotions.split(",") if e.strip())
    updates = {
        "llm_backend": llm_backend.strip(),
        "llm_base_url": llm_base_url.strip(),
        "llm_model": llm_model.strip(),
        "llm_temperature": llm_temperature.strip(),
        "evolution_alpha": evolution_alpha.strip(),
        "seed_deviation_band": seed_deviation_band.strip(),
        "trait_drift_delta": trait_drift_delta.strip(),
        "low_signal_emotions": emos,
    }
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
