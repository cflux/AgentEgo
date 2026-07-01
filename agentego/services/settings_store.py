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
    "round_exchanges": "3",
    "mood_lookback_rounds": "20",
    # Emotion/mood scoring backend: "llm" (combined Ollama call) or "goemotions" (local model).
    "scoring_backend": "llm",
    # Configurable emotion taxonomy: 28 GoEmotions labels + domain additions validated in testing.
    "emotion_taxonomy": (
        "admiration,amusement,anger,annoyance,approval,caring,confusion,curiosity,desire,"
        "disappointment,disapproval,disgust,embarrassment,excitement,fear,gratitude,grief,joy,"
        "love,nervousness,optimism,pride,realization,relief,remorse,sadness,surprise,neutral,"
        "arousal,lust,horny,yearning,longing,tenderness,affection,infatuation,passion,"
        "possessiveness,boredom,jealousy,contentment,trust,anticipation,awe,loneliness,contempt"
    ),
    "sentiment_llm_url": "http://localhost:11434",
    "sentiment_llm_model": "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest",
    # LLM mood predictions cast votes in the tally (per-round threshold voting).
    "llm_mood_votes_enabled": "1",
    "llm_mood_threshold": "6",
    "llm_mood_weight": "1",
    # Natural mood transitions: incumbent stickiness + penalty for non-adjacent "jumps".
    "mood_transitions_enabled": "1",
    "mood_inertia_bonus": "2",
    "mood_jump_penalty": "3",
    # Directed transition graph {mood_id: [moods it may move TO]}; the mood may only step to a
    # listed target (or stay) unless a non-listed mood's signal exceeds it by the jump penalty.
    "mood_adjacency": (
        '{"content":["social","curious","flirty","tired","sad","frustrated"],'
        '"social":["content","flirty","curious"],'
        '"flirty":["horny","content","social","curious"],'
        '"horny":["content","tired","flirty"],'
        '"curious":["content","social","focused","creative","flirty"],'
        '"focused":["curious","creative","tired","frustrated","content"],'
        '"creative":["curious","focused","content"],'
        '"tired":["content","sad","focused"],'
        '"frustrated":["content","tired","focused"],'
        '"sad":["content","tired"]}'
    ),
}


async def get_low_signal_emotions() -> set:
    """Emotions filtered out of the 'top' emotions (dominant GoEmotions noise)."""
    raw = await get_setting("low_signal_emotions", DEFAULTS["low_signal_emotions"])
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


async def get_mood_adjacency() -> dict:
    """Directed mood transition graph {mood_id: set(moods it may move TO)}. Directed so
    escalation (flirty→horny) and cooldown (horny→content) can differ."""
    import json
    raw = await get_setting("mood_adjacency", DEFAULTS["mood_adjacency"])
    try:
        graph = json.loads(raw or "{}")
    except (ValueError, TypeError):
        graph = {}
    return {m: set(ns or []) for m, ns in graph.items()}


async def get_transition_config() -> dict:
    """(enabled, inertia_bonus, jump_penalty, adjacency) for natural mood transitions."""
    enabled = (await get_setting("mood_transitions_enabled", "1")) == "1"
    try:
        inertia = max(0, int(float(await get_setting("mood_inertia_bonus", "2"))))
    except (TypeError, ValueError):
        inertia = 2
    try:
        penalty = max(0, int(float(await get_setting("mood_jump_penalty", "3"))))
    except (TypeError, ValueError):
        penalty = 3
    adjacency = await get_mood_adjacency()
    return {"enabled": enabled, "inertia": inertia, "penalty": penalty, "adjacency": adjacency}


async def get_emotion_taxonomy() -> list:
    """The configured emotion label list the scorer rates against (order preserved, deduped)."""
    raw = await get_setting("emotion_taxonomy", DEFAULTS["emotion_taxonomy"])
    seen, out = set(), []
    for e in (raw or "").replace("\n", ",").split(","):
        e = e.strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


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
