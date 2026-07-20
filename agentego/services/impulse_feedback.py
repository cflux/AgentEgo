"""Phase 5 — outward reply attribution (emotional consequence).

She reaches out; did Carbon respond? A scheduled pass classifies each outward ping **landed** (a user
message arrived in the DM within the conversation-gap window) or **ignored** (silence), binary. The signal
is emotional, not a frequency-learner:
  - a *landed* reply needs no special handling — it's a real conversation the mood engine already scores, and
    idle-reset clears solitude;
  - *ignored* pings (since Carbon was last around) add a decaying lonely bump (see solitude.py) and drive an
    outward backoff (see impulse_arbiter) — so silence stings and she stops knocking, without spamming.

"Ignored pings since he was last around" is the shared quantity: `ignored_pings_since_reengage` counts only
unanswered pings whose timestamp is after the last real user activity, so any re-engagement (a landed reply
*or* a Carbon-initiated message) naturally zeroes both the sting and the backoff.
"""
import json
import logging
import time

from ..db.ego import get_ego_db
from ..db.hermes import get_recent_sessions, get_session_messages_in_range
from .settings_store import get_setting

logger = logging.getLogger(__name__)


async def _conv_gap_seconds() -> float:
    try:
        return float(await get_setting("conv_gap_chat_minutes", "30")) * 60.0
    except (TypeError, ValueError):
        return 1800.0


async def _user_reply_in_window(db_path: str | None, T: float, gap: float) -> bool:
    """Was there a user message in (T, T+gap] on a chat platform? Reach-outs are delivered to a chat DM
    (telegram), and sessions rotate over time, so we can't just search 'the current session' — we scan any
    chat-platform (telegram/discord/…) session whose span overlaps the window. Non-chat sessions (cli,
    subagent) and cron/impulse turns are excluded."""
    from .conversations import _platform_of, _CHAT_PLATFORMS
    try:
        sessions = await get_recent_sessions(db_path=db_path)
    except Exception as exc:
        logger.warning("session scan failed: %s", exc)
        raise
    win_end = T + gap
    for s in sessions:
        sid = str(s.get("id") or "")
        if sid.startswith("cron_") or _platform_of(s.get("source")) not in _CHAT_PLATFORMS:
            continue
        started = s.get("started_at") or 0
        ended = s.get("ended_at") or win_end  # active session → treat as ongoing
        if ended < T or started > win_end:
            continue  # session span doesn't overlap the reply window
        msgs = await get_session_messages_in_range(sid, T + 0.001, win_end, db_path=db_path)
        if any(m.get("role") == "user" for m in msgs):
            return True
    return False


async def _rows(module: str) -> list[tuple]:
    conn = await get_ego_db()
    try:
        cur = await conn.execute("SELECT key, value FROM module_data WHERE module = ?", (module,))
        return await cur.fetchall()
    finally:
        await conn.close()


async def _outward_pings(profile: str) -> list[tuple]:
    out = []
    for key, val in await _rows("impulse_outcome"):
        try:
            rec = json.loads(val)
        except (ValueError, TypeError):
            continue
        if rec.get("profile") == profile and rec.get("kind") == "outward":
            out.append((key, rec))
    return out


async def _attributions(profile: str) -> dict:
    res = {}
    for key, val in await _rows("impulse_attribution"):
        try:
            rec = json.loads(val)
        except (ValueError, TypeError):
            continue
        if rec.get("profile") == profile:
            res[key] = rec
    return res


async def _save_attribution(key: str, rec: dict) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('impulse_attribution', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(rec), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


# ── the attribution pass ──────────────────────────────────────────────────────

async def attribute_profile(profile: str, db_path: str | None = None) -> int:
    """Resolve any outward pings whose reply-window has closed. Returns how many were newly resolved."""
    from .profiles import resolve_profile
    db_path = db_path or resolve_profile(profile)
    gap = await _conv_gap_seconds()
    now = time.time()
    done = await _attributions(profile)
    resolved = 0
    for key, rec in await _outward_pings(profile):
        if key in done:
            continue
        T = float(rec.get("at") or 0)
        if T <= 0 or (now - T) < gap:
            continue  # window still open — "no reply yet" must not read as ignored
        try:
            landed = await _user_reply_in_window(db_path, T, gap)
        except Exception:
            continue  # transient read error — retry next pass rather than mis-record
        await _save_attribution(key, {"profile": profile, "landed": landed, "at": T, "resolved_at": now})
        resolved += 1
    if resolved:
        logger.info("attributed %d outward ping(s) for %s", resolved, profile)
    return resolved


async def attribute_outward() -> None:
    """Scheduler entrypoint: attribute every profile's outward pings."""
    from .profiles import discover_profiles
    for p in discover_profiles():
        try:
            await attribute_profile(p["name"], db_path=p["db_path"])
        except Exception as exc:
            logger.warning("attribution failed for %s: %s", p.get("name"), exc)


# ── shared signal: unanswered pings since he was last around ──────────────────

async def ignored_pings_since_reengage(profile: str, db_path: str | None = None) -> list[float]:
    """Timestamps of ignored pings that happened *after* the last real user activity (newest last). Any
    re-engagement — a landed reply or a Carbon-initiated message — moves last-activity past them, zeroing
    both the lonely sting and the outward backoff."""
    from .impulse_engine import get_last_activity_ts
    last = await get_last_activity_ts(profile, db_path=db_path) or 0.0
    done = await _attributions(profile)
    ts = [float(r.get("at", 0)) for r in done.values()
          if not r.get("landed") and float(r.get("at", 0)) > last]
    return sorted(ts)


async def consecutive_ignored(profile: str, db_path: str | None = None) -> int:
    return len(await ignored_pings_since_reengage(profile, db_path=db_path))


async def attribution_summary(profile: str, db_path: str | None = None, limit: int = 30) -> dict:
    """Light readout for the panel: recent landed/ignored counts + the current unanswered streak."""
    done = await _attributions(profile)
    recs = sorted(done.values(), key=lambda r: float(r.get("at", 0)), reverse=True)[:limit]
    landed = sum(1 for r in recs if r.get("landed"))
    ignored = sum(1 for r in recs if not r.get("landed"))
    consec = await consecutive_ignored(profile, db_path=db_path)
    return {"landed": landed, "ignored": ignored, "consecutive_ignored": consec, "total": len(recs)}


async def recent_attributions(profile: str, limit: int = 15) -> list[dict]:
    """Recent reach-outs with their outcome, newest first: {at, landed, text, label}. Joins the
    attribution record to its ping (text + source-impulse label, both keyed by the ping's session_id)."""
    pings: dict = {}
    for key, val in await _rows("impulse_outcome"):
        try:
            rec = json.loads(val)
        except (ValueError, TypeError):
            continue
        if rec.get("profile") == profile and rec.get("kind") == "outward":
            pings[key] = {"text": rec.get("response", ""), "label": rec.get("label", "")}
    done = await _attributions(profile)
    items = [{"at": r.get("at"), "landed": bool(r.get("landed")),
              "text": pings.get(key, {}).get("text", ""), "label": pings.get(key, {}).get("label", "")}
             for key, r in done.items()]
    items.sort(key=lambda x: x["at"] or 0, reverse=True)
    items = items[:limit]
    # Backfill the source impulse for rows recorded before the fire→outcome label bridge existed.
    if any(not it["label"] for it in items):
        from .impulse_engine import class_fires, nearest_preceding_label
        fires = await class_fires(profile, outward=True)
        for it in items:
            if not it["label"]:
                it["label"] = nearest_preceding_label(fires, it["at"] or 0)
    return items
