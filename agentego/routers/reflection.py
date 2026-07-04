"""Daily reflection endpoints: the nightly pass is computed in reflection_engine; these serve the
stored result. Agent-facing surface mirrors the impulse relay (plain-text, EMPTY when nothing fires).
"""
import random
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates

from ..db.ego import get_ego_db
from ..services import reflection_engine, mood_engine
from ..services.profiles import discover_profiles, resolve_profile

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _moods() -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT id, name, color, icon FROM moods ORDER BY name")
        return [{"id": r[0], "name": r[1], "color": r[2], "icon": r[3]} for r in await cursor.fetchall()]
    finally:
        await conn.close()


async def _resting_mood(profile: str) -> str | None:
    """A resting mood for waking when no dream steers it: from the profile's default set."""
    moods = {m["id"]: m for m in await _moods()}
    defaults = await mood_engine._load_defaults(profile, moods)
    return random.choice(defaults) if defaults else None


# --- Agent-facing ---

@router.get("/api/reflection/dream.txt", response_class=PlainTextResponse)
async def dream_txt(profile: str = "default"):
    """The framed dream for today, delivered once. EMPTY if no dream (or already consumed) — an empty
    body lets a cron relay skip the agent entirely, exactly like impulse/next.txt."""
    r = await reflection_engine.get_today_reflection(profile)
    if not r or not r["dream_occurred"] or r["dream_consumed"] or not r["dream_prompt"]:
        return PlainTextResponse("")
    await reflection_engine.mark_dream_consumed(profile, r["day"])
    return PlainTextResponse(r["dream_prompt"])


@router.get("/api/reflection/dream")
async def dream_json(profile: str = "default") -> dict:
    r = await reflection_engine.get_today_reflection(profile)
    if not r or not r["dream_occurred"] or r["dream_consumed"] or not r["dream_prompt"]:
        return {"profile": profile, "dream": None}
    await reflection_engine.mark_dream_consumed(profile, r["day"])
    return {"profile": profile, "dream": r["dream_prompt"], "wake_mood": r["dream_mood_id"]}


@router.get("/api/reflection/wake")
async def wake(profile: str = "default") -> dict:
    """Reset the mood on waking: to the mood the dream left behind if one occurred, else a resting
    mood. Also returns the day's conclusions so the agent can read back its own general takeaways.
    The reset holds (via the mood-engine morning seed) until new activity arrives."""
    r = await reflection_engine.get_today_reflection(profile)
    from_dream = bool(r and r["dream_occurred"] and r["dream_mood_id"])
    target = r["dream_mood_id"] if from_dream else await _resting_mood(profile)
    reset = await mood_engine.reset_mood(profile, target) if target else None
    return {
        "profile": profile,
        "reset_to": reset["id"] if reset else None,
        "mood": reset["name"] if reset else None,
        "from_dream": from_dream,
        "conclusions": (r["conclusions"] if r else []),
        "den": (r["den"] if r else None),
    }


def _den_lines(den: dict) -> list[str]:
    """Format the Den digest for the agent: count + tags line, then the top-entry summaries."""
    if not den:
        return []
    n = den.get("count", 0)
    tags = ", ".join(den.get("tags", [])) or "—"
    lines = [f"You wrote {n} Den entr{'y' if n == 1 else 'ies'} yesterday. Tags: {tags}"]
    for e in den.get("top", []):
        imp = e.get("importance")
        lines.append(f"  • [{e.get('type', '?')}] {e.get('summary', '')}"
                     + (f" (importance {imp:.2f})" if isinstance(imp, (int, float)) else ""))
    return lines


@router.get("/api/reflection/wake.txt", response_class=PlainTextResponse)
async def wake_txt(profile: str = "default"):
    res = await wake(profile)
    lines = []
    if res["mood"]:
        lines.append(f"You wake feeling {res['mood']}"
                     + (" — the dream still clinging to you." if res["from_dream"] else "."))
    if res["conclusions"]:
        lines.append("Yesterday left you thinking:")
        lines += [f"- {c}" for c in res["conclusions"]]
    den_lines = _den_lines(res.get("den"))
    if den_lines:
        lines.append("")
        lines += den_lines
    return PlainTextResponse("\n".join(lines))


# --- Dashboard ---

@router.get("/reflections")
async def reflections_page(request: Request, profile: str = "default"):
    reflections = await reflection_engine.get_recent_reflections(profile)
    mood_map = {m["id"]: m for m in await _moods()}
    return templates.TemplateResponse(
        "reflections.html",
        {
            "request": request,
            "profiles": discover_profiles(),
            "active_profile": profile,
            "reflections": reflections,
            "mood_map": mood_map,
        },
    )


@router.post("/api/reflection/run")
async def run_now(request: Request, profile: str = "default"):
    """Run the reflection pass now (force, ignoring the activity gate) and re-render the panel."""
    await reflection_engine.reflect(profile, db_path=resolve_profile(profile), force=True)
    reflections = await reflection_engine.get_recent_reflections(profile)
    mood_map = {m["id"]: m for m in await _moods()}
    return templates.TemplateResponse(
        "partials/reflection_list.html",
        {"request": request, "reflections": reflections, "mood_map": mood_map},
    )
