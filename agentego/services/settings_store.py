"""Runtime-editable key/value settings backed by the app_settings table.

The model control panel writes here so the LLM backend can be changed without
editing env vars or restarting. Falls back to the migration-seeded defaults.
"""
import time
from ..db.ego import get_ego_db

# Keys exposed to the control panel, with their fallback defaults. Kept in sync
# with _DEFAULT_SETTINGS in db/migrations.py.
DEFAULTS = {
    "llm_backend": "deepseek",
    "llm_base_url": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "llm_temperature": "0.7",
    "evolution_alpha": "0.2",
    "seed_deviation_band": "0.35",
    "trait_drift_delta": "0.1",
    "impulse_enabled": "1",
    "impulse_restraint_weight": "0.5",
    "taste_pool_size": "15",
    "taste_sample_size": "5",
    "conv_gap_minutes": "120",
    "conv_gap_chat_minutes": "30",
    "low_signal_emotions": "neutral,approval",
}


async def get_low_signal_emotions() -> set:
    """Emotions filtered out of the 'top' emotions (dominant GoEmotions noise)."""
    raw = await get_setting("low_signal_emotions", DEFAULTS["low_signal_emotions"])
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


async def get_all_settings() -> dict:
    """Return every known setting, filling any gaps with defaults."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT key, value FROM app_settings")
        stored = {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await conn.close()
    return {k: stored.get(k, default) for k, default in DEFAULTS.items()}


async def get_setting(key: str, default: str | None = None) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if row is not None:
        return row[0]
    return default if default is not None else DEFAULTS.get(key)


async def set_settings(updates: dict) -> None:
    """Upsert a batch of settings. Empty string values are written as-is so the
    panel can clear a field; callers should skip keys they don't want to touch."""
    now = time.time()
    conn = await get_ego_db()
    try:
        for key, value in updates.items():
            if key not in DEFAULTS:
                continue
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        await conn.commit()
    finally:
        await conn.close()


async def get_llm_config() -> dict:
    """Resolved LLM connection config for the client + worker."""
    s = await get_all_settings()

    def _f(key: str, fallback: float) -> float:
        try:
            return float(s.get(key))
        except (TypeError, ValueError):
            return fallback

    return {
        "backend": s.get("llm_backend") or "deepseek",
        "base_url": (s.get("llm_base_url") or "").rstrip("/"),
        "api_key": s.get("llm_api_key") or "",
        "model": s.get("llm_model") or "",
        "temperature": _f("llm_temperature", 0.7),
    }


async def get_evolution_config() -> dict:
    """Bounded-evolution tuning knobs."""
    s = await get_all_settings()

    def _f(key: str, fallback: float) -> float:
        try:
            return float(s.get(key))
        except (TypeError, ValueError):
            return fallback

    return {
        "alpha": _f("evolution_alpha", 0.2),
        "seed_band": _f("seed_deviation_band", 0.35),
        "trait_drift": _f("trait_drift_delta", 0.1),
    }
