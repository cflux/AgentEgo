"""Impulse v2 arbiter — the generative replacement for the v1 weighted lottery.

On a cron check-in AgentEgo assembles the agent's current *state* (mood, tastes, what's been on her
mind via the Den + reflections, idle time, and — for outward — the recent conversation gist) and an
LLM decides "what, if anything, do I feel like doing right now?", choosing among the *enabled*
capabilities in the manifest (settings_store.get_impulse_capabilities) and composing the specific
impulse prompt itself. The manifest is the correctness fence: the arbiter may only pick something the
agent can actually do in a cron turn.

Two classes, driven by separate cron jobs with their own cadence:
  - inward  (explore / create / write-den): runs silently, persists via the Den, no delivery.
  - outward (reach-out): composes a message; delivered + mirrored by the agentego-impulse plugin.

Scoped per call: the inward job only considers inward capabilities, the outward job only outward —
so the frequent inward job can't spam DMs. Returns a composed prompt (or "" / not-fired for nothing).
"""
import time

from .settings_store import get_setting, get_impulse_capabilities
from .llm_client import chat, LLMError

# Marker the arbiter embeds in an outward prompt; the agentego-impulse plugin keys its DM-mirror path
# off the same string. Keep in sync with the plugin's OUTWARD_MARKER.
OUTWARD_MARKER = "[IMPULSE-OUTWARD]"

INWARD_INTENTS = {"explore", "create", "write-den"}
OUTWARD_INTENTS = {"reach-out"}

_DEN_INSTRUCTION = (
    "\n\nThis is a private, self-directed moment — no one is watching. Carry it out fully with your "
    "tools, then record what you did and how it felt as a short entry in your Den so you remember it."
)

# Appended to inward prompts so the sidequest ends with a first-party feeling read. The entry mood is
# framed as a baseline to move FROM (not confirm), and "or if it didn't" avoids manufacturing change — this
# is input #1 of the three-input sidequest scorer.
_SELF_REPORT_TEMPLATE = (
    "\n\nWhen you're done, on its own final line, honestly finish this: you came into this feeling "
    "{mood} — now that it's over, in one line, how do you feel? Say if it shifted, or if it didn't."
)


def _self_report_instruction(facts: dict) -> str:
    mood = (facts.get("mood") or {}).get("name") or "however you were"
    return _SELF_REPORT_TEMPLATE.format(mood=mood)


def _class_of(cap: dict) -> str:
    """A capability's class ('inward'|'outward'). Explicit `class` wins; otherwise derive from the legacy
    `intent` (backward-compat for manifests predating the class field)."""
    c = str(cap.get("class") or "").strip().lower()
    if c in ("inward", "outward"):
        return c
    return "outward" if cap.get("intent") in OUTWARD_INTENTS else "inward"


async def _idle_minutes(profile: str, db_path: str | None) -> float | None:
    from .impulse_engine import get_last_activity_ts
    last_ts = await get_last_activity_ts(profile, db_path=db_path)
    if not last_ts:
        return None
    return round((time.time() - last_ts) / 60.0, 1)


async def recent_gist(profile: str, db_path: str | None = None, max_turns: int = 8) -> str:
    """A short recent user/agent transcript (excludes cron/sidequest sessions). Shared by the arbiter
    (outward state) and the /api/impulse/brief endpoint so both use one implementation."""
    from ..db.hermes import get_recent_sessions, get_session_messages
    from .profiles import resolve_profile
    db_path = db_path or resolve_profile(profile)

    def _is_cron_session(s: dict) -> bool:
        if str(s.get("id") or "").startswith("cron_"):
            return True
        return "cron" in str(s.get("source") or "").lower()

    try:
        sessions = [s for s in await get_recent_sessions(db_path=db_path) if not _is_cron_session(s)]
        if not sessions:
            return ""
        msgs = await get_session_messages(sessions[0]["id"], db_path=db_path)
    except Exception:
        return ""
    turns = [m for m in msgs if m.get("role") in ("user", "assistant") and m.get("content")]
    return "\n".join(f"{m['role']}: {m['content'][:300]}" for m in turns[-max_turns:])


async def _assemble_state(profile: str, cls: str, db_path: str | None,
                          idle_min: float | None) -> tuple[str, dict]:
    """Build the free-text 'here is how you are right now' block for the arbiter, plus a small dict of
    structured facts for the API/dashboard. Reuses the same enrichment the reflection engine draws on."""
    from .mood_engine import get_cached_mood
    from .affinity_engine import get_taste_context
    from .reflection_engine import get_today_reflection, get_recent_reflections
    from .impulse_engine import get_recent_log
    from . import den

    parts: list[str] = []
    facts: dict = {}

    mood = await get_cached_mood(profile)
    if mood:
        facts["mood"] = {"id": mood["id"], "name": mood["name"]}
        desc = f" ({mood['description']})" if mood.get("description") else ""
        parts.append(f"Right now you feel **{mood['name']}**{desc}.")

    try:
        taste = await get_taste_context(profile, sample=True)
    except Exception:
        taste = {}
    if taste.get("interests") and taste["interests"] != "—":
        parts.append(f"Things you're drawn to lately: {taste['interests']}.")
    if taste.get("likes") and taste["likes"] != "—":
        parts.append(f"You like: {taste['likes']}.")
    if taste.get("dislikes") and taste["dislikes"] != "—":
        parts.append(f"You dislike: {taste['dislikes']}.")

    # What's been on her mind — today's reflection conclusions, else the most recent day's.
    today = await get_today_reflection(profile)
    conclusions = (today or {}).get("conclusions") or []
    if not conclusions:
        for r in await get_recent_reflections(profile, limit=3):
            if r.get("conclusions"):
                conclusions = r["conclusions"]
                break
    if conclusions:
        parts.append("Lately you've been thinking:\n- " + "\n- ".join(conclusions[:4]))

    # Recent Den entries = meaning she's been building.
    try:
        entries = den.list_entries(profile)[:4]
    except Exception:
        entries = []
    den_lines = [f"{e['date']}: {e['summary'] or e['slug']}" for e in entries if (e.get("summary") or e.get("slug"))]
    if den_lines:
        parts.append("Recent things in your Den:\n- " + "\n- ".join(den_lines))

    # Anti-repetition: what you've already done on your own recently.
    recent = [x["label"] for x in await get_recent_log(profile, limit=8) if x.get("label")]
    if recent:
        parts.append("Things you did on your own recently (don't just repeat these): " + ", ".join(recent))

    if idle_min is not None:
        facts["idle_minutes"] = idle_min
        hrs = idle_min / 60.0
        when = f"{int(idle_min)} min" if idle_min < 90 else f"{hrs:.1f} h"
        parts.append(f"It's been {when} since you last talked with the user.")

    if cls == "outward":
        gist = await recent_gist(profile, db_path)
        if gist:
            parts.append("Where things stand with the user right now (recent messages):\n" + gist)

    return "\n\n".join(parts), facts


def _capability_menu(caps: list) -> str:
    return "\n".join(f"- {c['id']}: {c['description']}" for c in caps)


async def arbitrate(profile: str, cls: str, *, db_path: str | None = None, commit: bool = True) -> dict:
    """Decide whether/what the agent does on its own for class `cls` ('inward'|'outward').

    Returns {fired, cls, capability_id, intent, prompt, mood, idle_minutes, capabilities, reason}.
    `prompt` is the composed impulse (with the operational hint prepended, the outward marker or Den
    instruction added). commit=True logs a fire to impulse_log; commit=False is a dry-run."""
    cls = "outward" if cls == "outward" else "inward"
    caps = [c for c in await get_impulse_capabilities(enabled_only=True) if _class_of(c) == cls]
    idle_min = await _idle_minutes(profile, db_path)

    result = {
        "profile": profile, "cls": cls, "fired": False, "capability_id": None, "intent": None,
        "prompt": None, "mood": None, "idle_minutes": idle_min,
        "capabilities": [c["id"] for c in caps], "reason": None,
    }

    enabled_global = (await get_setting("impulse_enabled", "1")) == "1"
    if not enabled_global:
        result["reason"] = "impulse system disabled"
        return result
    if not caps:
        result["reason"] = f"no enabled {cls} capabilities"
        return result

    # Quiet-hours gate (both classes; container TZ = Pacific). Inward defaults to 06:00-24:00 (a
    # 00:00-06:00 "sleep" blackout); outward to daytime 06:00-20:00. Wrap-around supported (h0 > h1).
    from datetime import datetime
    _dflt_end = "24" if cls == "inward" else "20"
    try:
        h0 = int(await get_setting(f"impulse_{cls}_hour_start", "6"))
        h1 = int(await get_setting(f"impulse_{cls}_hour_end", _dflt_end))
    except (TypeError, ValueError):
        h0, h1 = 6, (24 if cls == "inward" else 20)
    hour = datetime.now().hour
    in_window = (h0 <= hour < h1) if h0 <= h1 else (hour >= h0 or hour < h1)
    if not in_window:
        result["reason"] = f"outside {cls} hours ({h0:02d}:00-{h1:02d}:00 local; now {hour:02d}:00)"
        return result

    if cls == "outward":
        # Mid-conversation guard — a hard idle gate so an outward DM can't fire while the user is
        # actively chatting. idle_min excludes cron/impulse turns; None = no recent user activity (fine).
        try:
            min_idle = float(await get_setting("impulse_outward_min_idle_minutes", "30"))
        except (TypeError, ValueError):
            min_idle = 30.0
        if idle_min is not None and idle_min < min_idle:
            result["reason"] = f"user active {idle_min:.0f} min ago (< {min_idle:.0f} min idle gate) - not interrupting"
            return result

    state_text, facts = await _assemble_state(profile, cls, db_path, idle_min)
    result["mood"] = facts.get("mood")

    system = await get_setting("impulse_arbiter_prompt", "")
    persona = ""
    try:
        from .reflection_engine import _persona_block
        persona = await _persona_block(profile)
    except Exception:
        persona = f"Character profile: {profile}."

    user = (
        f"{persona}\n\n--- HOW YOU ARE RIGHT NOW ---\n{state_text or '(a quiet, unremarkable moment)'}\n\n"
        f"--- WHAT YOU CAN DO RIGHT NOW ---\n{_capability_menu(caps)}\n\n"
        "Pick exactly one capability id, or \"none\"."
    )

    try:
        from .reflection_engine import _parse_json
        raw = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         response_json=True, max_tokens=600)
        data = _parse_json(raw)
    except LLMError as exc:
        result["reason"] = f"arbiter LLM error: {exc}"
        return result

    cap_id = str(data.get("capability_id") or "").strip()
    composed = str(data.get("prompt") or "").strip()
    if cap_id in ("", "none", "nothing") or not composed:
        result["reason"] = "chose to stay quiet"
        return result

    cap = next((c for c in caps if c["id"] == cap_id), None)
    if not cap:
        result["reason"] = f"picked unknown/disallowed capability '{cap_id}'"
        return result

    hint = (cap.get("operational_hint") or "").strip()
    prompt = f"{composed}\n\n({hint})" if hint else composed
    if cls == "outward":
        prompt = f"{OUTWARD_MARKER}\n{prompt}"
    else:
        prompt = prompt + _DEN_INSTRUCTION + _self_report_instruction(facts)

    result.update(fired=True, capability_id=cap_id, intent=cap.get("intent"), prompt=prompt,
                  reason="acting")

    if commit:
        from .impulse_engine import _log_fire
        mood_id = (facts.get("mood") or {}).get("id")
        action = {"id": cap_id, "label": cap.get("intent") or cap_id}
        await _log_fire(profile, action, prompt, mood_id, idle_min or 0.0)

    return result
