"""AgentEgo impulse bridge plugin (Phase 1).

Hooks (both cron-gated — inert for live gateway turns):
  - pre_llm_call:  on an impulse cron turn, fetch AgentEgo's context brief and inject it (the cron
                   session is cold: no mnemosyne, no live history).
  - post_llm_call: forward the outcome to AgentEgo (for sidequest scoring) and, for OUTWARD impulses,
                   mirror the message into the user's DM session transcript so the agent remembers it.
                   Hermes' native deliver=telegram:<dm> performs the actual send; this adds the mirror
                   that auto-delivery omits.

Outward vs inward is signalled by a marker AgentEgo embeds in the impulse prompt (OUTWARD_MARKER).
Sync handlers, stdlib-only HTTP.
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

EGO_URL = os.environ.get("EGO_URL", "http://127.0.0.1:8765").rstrip("/")
OUTWARD_MARKER = "[IMPULSE-OUTWARD]"
_PROFILE = "default"


def _get(url: str, timeout: float = 4.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url: str, payload: dict, timeout: float = 4.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _dm_chat_id() -> str:
    """Resolve the user's Telegram DM chat_id. Prefer an explicit override, else derive it from the
    profile's sessions index (the agent:main:telegram:dm:<id> entry), else TELEGRAM_ALLOWED_USERS."""
    cid = os.environ.get("IMPULSE_DM_CHAT_ID", "").strip()
    if cid:
        return cid
    try:
        from hermes_cli.config import get_hermes_home
        idx = json.load(open(get_hermes_home() / "sessions" / "sessions.json"))
        for key, entry in idx.items():
            origin = entry.get("origin") or {}
            platform = (origin.get("platform") or entry.get("platform", "")).lower()
            if platform == "telegram" and ":dm:" in key:
                return str(origin.get("chat_id") or "")
    except Exception:
        pass
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
    return allowed.split(",")[0].strip() if allowed else ""


def _pre_llm_call(**kwargs):
    if kwargs.get("platform") != "cron":
        return None
    prompt = kwargs.get("user_message") or ""
    kind = "outward" if OUTWARD_MARKER in prompt else "inward"
    try:
        brief = _get(f"{EGO_URL}/api/impulse/brief?profile={_PROFILE}&kind={kind}")
        ctx_text = (brief or {}).get("context") or ""
        if ctx_text.strip():
            return {"context": ctx_text}
    except Exception as exc:
        logger.warning("impulse brief fetch failed: %s", exc)
    return None


def _post_llm_call(**kwargs):
    if kwargs.get("platform") != "cron":
        return None
    prompt = kwargs.get("user_message") or ""
    resp = (kwargs.get("assistant_response") or "").strip()
    session_id = kwargs.get("session_id") or ""
    outward = OUTWARD_MARKER in prompt
    kind = "outward" if outward else "inward"

    # Forward the outcome for scoring (best-effort).
    try:
        _post(f"{EGO_URL}/api/impulse/outcome",
              {"profile": _PROFILE, "session_id": session_id, "kind": kind, "response": resp})
    except Exception as exc:
        logger.warning("impulse outcome forward failed: %s", exc)

    # Mirror OUTWARD messages into the DM session transcript (native delivery skips this).
    if outward and resp and resp != "[SILENT]":
        try:
            from gateway.mirror import mirror_to_session
            chat_id = _dm_chat_id()
            if chat_id:
                mirror_to_session(platform="telegram", chat_id=chat_id, message_text=resp,
                                  source_label="impulse")
            else:
                logger.warning("impulse mirror skipped: no DM chat_id resolved")
        except Exception as exc:
            logger.warning("impulse mirror failed: %s", exc)
    return None


def register(ctx):
    global _PROFILE
    _PROFILE = getattr(ctx, "profile_name", None) or "default"
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    logger.info("agentego-impulse registered (profile=%s)", _PROFILE)
