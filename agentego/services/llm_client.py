"""Thin OpenAI-compatible chat client.

Reads backend/model/key/url/temperature from app_settings (see settings_store).
Works against the DeepSeek API (default) or any OpenAI-compatible endpoint,
including a local Ollama instance pointed at its /v1 base url.
"""
import json
import httpx
from .settings_store import get_llm_config


class LLMError(Exception):
    pass


async def chat(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int = 800,
    response_json: bool = False,
    timeout: float = 60.0,
) -> str:
    """Send a chat completion and return the assistant message content.

    `response_json=True` asks the backend for a JSON object (DeepSeek/OpenAI
    support response_format; harmless to others which simply ignore it via
    our try/except fallback)."""
    cfg = await get_llm_config()
    if not cfg["base_url"]:
        raise LLMError("No LLM base_url configured — set one in the control panel.")
    if not cfg["model"]:
        raise LLMError("No LLM model configured — set one in the control panel.")

    payload: dict = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"] if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    url = f"{cfg['base_url']}/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            detail = str(e) or e.__class__.__name__
            raise LLMError(f"Request failed: {detail}") from e
    if resp.status_code >= 400:
        raise LLMError(f"{resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"Unexpected response shape: {e}") from e


async def ping() -> dict:
    """Cheap connectivity check for the control panel's Test button."""
    import time
    start = time.time()
    try:
        content = await chat(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
            timeout=20.0,
        )
        return {"ok": True, "latency_ms": int((time.time() - start) * 1000), "reply": content.strip()[:40]}
    except LLMError as e:
        return {"ok": False, "error": str(e)}
