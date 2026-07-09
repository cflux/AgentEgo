"""AgentEgo impulse bridge plugin (Phase 3).

Hooks:
  - pre_llm_call:  on cron turns, fetches AgentEgo's context brief; on live DM
                   turns, injects pending impulse catch-up from cache (both
                   outward and inward).
  - post_llm_call: on cron turns, forwards outcome to AgentEgo; caches both
                   outward and inward impulse responses for the next live turn.

Outward vs inward signalled by OUTWARD_MARKER in the impulse prompt.
Sync handlers, stdlib-only HTTP.
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

EGO_URL = os.environ.get("EGO_URL", "http://127.0.0.1:8765").rstrip("/")
OUTWARD_MARKER = "[IMPULSE-OUTWARD]"
_PROFILE = "default"
_CACHE = "/tmp/{}_pending_impulses.json"


def _get(url: str, timeout: float = 4.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url: str, payload: dict, timeout: float = 4.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cache_path():
    return _CACHE.format(_PROFILE)


def _load_cache():
    """Read the pending-impulse cache, tolerating a missing/empty/corrupt file (-> [])."""
    path = _cache_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = f.read().strip()
        parsed = json.loads(data) if data else []
        return parsed if isinstance(parsed, list) else []
    except (ValueError, OSError):
        return []


def _save_cache(impulses):
    """Write the cache atomically-ish; always leaves valid JSON (never a 0-byte file)."""
    try:
        with open(_cache_path(), "w") as f:
            json.dump(impulses, f)
    except OSError as exc:
        logger.warning("impulse cache write failed: %s", exc)


# ── pre_llm_call ────────────────────────────────────────────────────────────

def _pre_llm_call(**kwargs):
    platform = kwargs.get("platform", "")

    # Cron turn: fetch AgentEgo's context brief (cold session — no mnemosyne)
    if platform == "cron":
        prompt = kwargs.get("user_message") or ""
        kind = "outward" if OUTWARD_MARKER in prompt else "inward"
        try:
            brief = _get(f"{EGO_URL}/api/impulse/brief?profile={_PROFILE}&kind={kind}")
            ctx = (brief or {}).get("context") or ""
            if ctx.strip():
                return {"context": ctx}
        except Exception as exc:
            logger.warning("impulse brief fetch failed: %s", exc)
        return None

    # Live DM turn: inject pending impulse catch-up (outward + inward), then clear the cache.
    try:
        impulses = _load_cache()
        if not impulses:
            return None

        outward = [i for i in impulses if i.get("kind", "outward") == "outward"]
        inward  = [i for i in impulses if i.get("kind") == "inward"]

        lines = []
        if outward:
            lines.append("## While Carbon was away, you reached out to him:")
            for imp in outward[-10:]:
                lines.append(f"- {imp.get('content', '')[:300]}")
            lines.append("")
        if inward:
            lines.append("## While you were alone, you did these on your own:")
            for imp in inward[-10:]:
                lines.append(f"- {imp.get('content', '')[:300]}")
            lines.append("")

        if not lines:
            return None

        _save_cache([])  # consumed — clear so the next turn isn't caught up twice
        logger.info("impulse catch-up: %d outward, %d inward", len(outward), len(inward))
        return {"context": "\n".join(lines)}
    except Exception as exc:
        logger.warning("impulse catch-up failed: %s", exc)

    return None


# ── post_llm_call ───────────────────────────────────────────────────────────

def _post_llm_call(**kwargs):
    if kwargs.get("platform") != "cron":
        return None
    prompt = kwargs.get("user_message") or ""
    resp = (kwargs.get("assistant_response") or "").strip()
    session_id = kwargs.get("session_id") or ""
    outward = OUTWARD_MARKER in prompt
    kind = "outward" if outward else "inward"

    # Forward outcome to AgentEgo (best-effort).
    try:
        _post(f"{EGO_URL}/api/impulse/outcome",
              {"profile": _PROFILE, "session_id": session_id, "kind": kind, "response": resp})
    except Exception as exc:
        logger.warning("impulse outcome forward failed: %s", exc)

    # Cache both outward AND inward impulses — next live DM turn catches her up.
    if resp and resp != "[SILENT]":
        impulses = _load_cache()
        impulses.append({"content": resp, "timestamp": time.time(), "kind": kind})
        _save_cache(impulses)
        logger.info("impulse cached (%s): %d pending", kind, len(impulses))

    return None


# ── register ────────────────────────────────────────────────────────────────

def register(ctx):
    global _PROFILE
    _PROFILE = getattr(ctx, "profile_name", None) or "default"
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    logger.info("agentego-impulse registered (profile=%s)", _PROFILE)
