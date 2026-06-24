import os
import time
import aiohttp

EGO_URL = os.environ.get("EGO_BRIDGE_URL", "http://127.0.0.1:8765/api/events")
_TIMEOUT = aiohttp.ClientTimeout(connect=2.0, total=5.0)
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)
    return _session


async def handle(event_type: str, context: dict) -> None:
    payload = {
        "event_type": event_type,
        "session_id": context.get("session_id"),
        "platform": context.get("platform", ""),
        "user_id": context.get("user_id", ""),
        "chat_id": context.get("chat_id", ""),
        "timestamp": time.time(),
    }
    try:
        sess = _get_session()
        async with sess.post(EGO_URL, json=payload) as resp:
            if resp.status >= 400:
                print(f"[ego-bridge] HTTP {resp.status} from AgentEgo", flush=True)
    except Exception as exc:
        print(f"[ego-bridge] Failed to forward {event_type}: {exc}", flush=True)
