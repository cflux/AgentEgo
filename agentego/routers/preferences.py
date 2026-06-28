import json
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

from ..db.ego import get_ego_db
from ..services.profiles import discover_profiles, resolve_profile
from ..services.conversations import sync_recent_conversations
from ..services import affinity_engine
from ..services.llm_client import chat, LLMError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _unit(x, signed: bool = False) -> float:
    """Normalize a model score to 0..1 (or -1..1 if signed), rescaling 0-100 answers."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if abs(v) > 1.0:
        v = v / 100.0
    return max(-1.0 if signed else 0.0, min(1.0, v))


# --- Worker payload models ---

class SeedAffinity(BaseModel):
    entity: str
    category: Optional[str] = None
    valence: float = 0.0
    intensity: float = 0.5
    confidence: float = 0.7
    rationale: Optional[str] = None


class TraitsPayload(BaseModel):
    profile: str = "default"
    source_hash: str
    traits: dict
    seeds: list[SeedAffinity] = []


class AffinityPayload(BaseModel):
    profile: str = "default"
    entity: str
    category: Optional[str] = None
    valence: float = 0.0
    intensity: float = 0.5
    confidence: float = 0.5
    rationale: Optional[str] = None
    source: str = "inferred"


# --- _system flag helpers (mirror sentiment/topic worker plumbing) ---

async def _set_flag(key: str, value: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO module_data (module, key, value, updated_at)
            VALUES ('_system', ?, ?, ?)
            ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


# --- Worker-facing API ---

@router.get("/api/preferences/profiles")
async def list_profiles() -> list[str]:
    """Profile names the worker should process (filesystem auto-discovery)."""
    return [p["name"] for p in discover_profiles()]


@router.get("/api/preferences/trait-status")
async def trait_status(profile: str = "default") -> dict:
    """Worker compares its freshly computed SOUL.md hash against the stored one."""
    traits = await affinity_engine.get_traits(profile)
    return {
        "profile": profile,
        "has_traits": traits is not None,
        "source_hash": traits["source_hash"] if traits else None,
    }


@router.post("/api/preferences/traits", status_code=202)
async def save_traits(payload: TraitsPayload):
    await affinity_engine.save_traits(payload.profile, payload.source_hash, payload.traits)
    for seed in payload.seeds:
        await affinity_engine.apply_observation(
            payload.profile, seed.entity,
            valence=seed.valence, intensity=seed.intensity, confidence=seed.confidence,
            category=seed.category, rationale=seed.rationale, source="seed",
        )
    return {"status": "saved", "seeds": len(payload.seeds)}


@router.get("/api/preferences/pending")
async def get_pending(profile: str = "default") -> dict:
    """Entities needing inference plus the traits substrate to reason with.

    Deliberately returns the abstracted traits — NOT the SOUL text — so the worker
    must extrapolate rather than echo the persona's stated likes."""
    await sync_recent_conversations(profile, db_path=resolve_profile(profile))
    traits = await affinity_engine.get_traits(profile)
    entities = await affinity_engine.get_pending_entities(profile)
    return {
        "profile": profile,
        "traits": traits["current"] if traits else None,
        "entities": entities,
    }


@router.post("/api/preferences/affinity", status_code=202)
async def save_affinity(payload: AffinityPayload):
    result = await affinity_engine.apply_observation(
        payload.profile, payload.entity,
        valence=payload.valence, intensity=payload.intensity, confidence=payload.confidence,
        category=payload.category, rationale=payload.rationale, source=payload.source,
    )
    return {"status": "saved", **result}


@router.post("/api/preferences/heartbeat", status_code=202)
async def worker_heartbeat():
    await _set_flag("preference_heartbeat", "1")
    return {"status": "ok"}


@router.post("/api/preferences/progress", status_code=202)
async def update_progress(current: int, total: int, entity: str = ""):
    await _set_flag("preference_progress", json.dumps({"current": current, "total": total, "entity": entity}))
    return {"status": "ok"}


@router.post("/api/preferences/complete", status_code=202)
async def run_complete():
    await _set_flag("preference_complete", "1")
    return {"status": "ok"}


@router.post("/api/preferences/trigger", status_code=202)
async def trigger_run():
    await _set_flag("preference_trigger", "1")
    return JSONResponse({"status": "queued"}, headers={"HX-Trigger": "preferenceUpdate"})


@router.post("/api/preferences/trigger-clear", status_code=202)
async def clear_trigger():
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE module_data SET value='0' WHERE module='_system' AND key='preference_trigger'"
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "cleared"}


async def preference_status() -> dict:
    """Aggregate status for the dashboard/preferences panel."""
    profiles = discover_profiles()
    pending = 0
    missing_traits = []
    for p in profiles:
        traits = await affinity_engine.get_traits(p["name"])
        if traits is None:
            missing_traits.append(p["name"])
        try:
            pending += len(await affinity_engine.get_pending_entities(p["name"]))
        except Exception:
            pass

    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='_system' AND key='preference_trigger'"
        )
        row = await cursor.fetchone()
        triggered = row is not None and row[0] == "1"

        cursor = await conn.execute("SELECT MAX(updated_at) FROM affinities")
        last_row = await cursor.fetchone()
        last_run = last_row[0] if last_row and last_row[0] else None

        cursor = await conn.execute(
            "SELECT updated_at FROM module_data WHERE module='_system' AND key='preference_heartbeat'"
        )
        hb_row = await cursor.fetchone()
        # The preference worker polls every 5 min (vs. 60s for sentiment/topic), so its
        # heartbeat window is wider; it also beats every ~30s while idle once updated.
        worker_online = hb_row is not None and (time.time() - hb_row[0]) < 330

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='preference_progress'"
        )
        prog_row = await cursor.fetchone()
        progress = None
        if prog_row and (time.time() - prog_row[1]) < 30:
            try:
                progress = json.loads(prog_row[0])
                if progress.get("total", 0) == 0:
                    progress = None
            except Exception:
                pass

        cursor = await conn.execute(
            "SELECT value, updated_at FROM module_data WHERE module='_system' AND key='preference_complete'"
        )
        complete_row = await cursor.fetchone()
        just_completed = (
            complete_row is not None and complete_row[0] == "1"
            and (time.time() - complete_row[1]) < 10
        )
        if just_completed:
            await conn.execute(
                "UPDATE module_data SET value='0' WHERE module='_system' AND key='preference_complete'"
            )
            await conn.commit()
    finally:
        await conn.close()

    return {
        "pending": pending,
        "triggered": triggered,
        "last_run": last_run,
        "worker_online": worker_online,
        "progress": progress,
        "just_completed": just_completed,
        "missing_traits": missing_traits,
    }


@router.get("/api/preferences/status")
async def status_endpoint() -> dict:
    return await preference_status()


# --- Page + dev playground ---

@router.get("/preferences")
async def preferences_page(request: Request, profile: str = "default"):
    profiles = discover_profiles()
    traits = await affinity_engine.get_traits(profile)
    summary = await affinity_engine.get_affinity_summary(profile)
    status = await preference_status()
    return templates.TemplateResponse(
        "preferences.html",
        {
            "request": request,
            "profiles": profiles,
            "active_profile": profile,
            "traits": traits,
            "summary": summary,
            "status": status,
            "ocean_keys": affinity_engine.OCEAN_KEYS,
        },
    )


@router.get("/partials/preference-status")
async def preference_status_partial(request: Request):
    status = await preference_status()
    headers = {"HX-Trigger": "preferenceComplete"} if status.get("just_completed") else {}
    return templates.TemplateResponse(
        "partials/preference_status.html",
        {"request": request, "status": status},
        headers=headers,
    )


_OPINION_SYSTEM = (
    "You ARE a character defined ONLY by the personality traits below — NOT by any list of "
    "stated likes. Form a genuine opinion about the subject the user names by reasoning from "
    "these traits and values. The subject may be something never mentioned in your background; "
    "extrapolate from who you are. Do not refuse, do not hedge as an AI.\n\n"
    "Respond with a JSON object ONLY:\n"
    '{"valence": <float -1..1, dislike..like>, "intensity": <float 0..1, how strongly you feel>, '
    '"category": "<one word: object|activity|concept|person|place|topic|food|media>", '
    '"rationale": "<1 sentence, why — grounded in your traits>", '
    '"in_character_line": "<one short in-character reaction, your actual voice>"}'
)


async def _infer_opinion(profile: str, subject: str) -> dict:
    """Trait-grounded LLM opinion on a subject. Raises LLMError / ValueError on failure."""
    traits = await affinity_engine.get_traits(profile)
    if not traits:
        raise ValueError(f"No personality traits extracted for '{profile}' yet — run the worker first.")
    current = traits["current"]
    ocean = current.get("ocean", {})
    trait_block = (
        f"Trait summary: {current.get('summary', '')}\n"
        f"OCEAN: " + ", ".join(f"{k}={ocean.get(k)}" for k in affinity_engine.OCEAN_KEYS) + "\n"
        f"Core values: {', '.join(current.get('values', []))}"
    )
    raw = await chat(
        [{"role": "system", "content": _OPINION_SYSTEM},
         {"role": "user", "content": f"{trait_block}\n\nSubject to form an opinion about: {subject}"}],
        response_json=True,
        max_tokens=2000,
    )
    data = json.loads(raw)  # JSONDecodeError bubbles up
    return {
        "subject": subject,
        "valence": _unit(data.get("valence"), signed=True),
        "intensity": _unit(data.get("intensity")) or 0.5,
        "category": data.get("category"),
        "rationale": data.get("rationale", ""),
        "in_character_line": data.get("in_character_line", ""),
    }


@router.post("/api/preferences/opinion")
async def opinion(request: Request, profile: str = Form("default"), subject: str = Form(...), save: bool = Form(False)):
    """Dev playground: live trait-grounded opinion on an arbitrary subject (HTML)."""
    subject = subject.strip()
    if not subject:
        return JSONResponse({"error": "empty subject"}, status_code=400)
    try:
        result = await _infer_opinion(profile, subject)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except json.JSONDecodeError:
        return JSONResponse({"error": "model did not return valid JSON"}, status_code=502)

    if save:
        await affinity_engine.apply_observation(
            profile, subject,
            valence=float(result["valence"]), intensity=float(result["intensity"]),
            confidence=0.5, category=result["category"], rationale=result["rationale"],
            source="observed",
        )
        result["saved"] = True

    return templates.TemplateResponse(
        "partials/opinion_result.html",
        {"request": request, "r": result},
    )


# --- Agent-facing JSON API ---

@router.get("/api/preferences/profile")
async def preference_profile(profile: str = "default") -> dict:
    """The agent's taste profile: personality + top likes/dislikes/interests."""
    taste = await affinity_engine.get_taste_context(profile, top_n=10)
    traits = taste.get("traits")
    return {
        "profile": profile,
        "personality": taste["personality"],
        "values": taste["values"],
        "ocean": (traits["current"].get("ocean") if traits else None),
        "likes": [{"thing": a["entity"], "valence": a["valence"], "intensity": a["intensity"]}
                  for a in taste["summary"]["likes"]],
        "dislikes": [{"thing": a["entity"], "valence": a["valence"], "intensity": a["intensity"]}
                     for a in taste["summary"]["dislikes"]],
        "interests": [{"thing": a["entity"], "intensity": a["intensity"]}
                      for a in taste["summary"]["interests"]],
    }


@router.get("/api/preferences/opinion")
async def opinion_json(profile: str = "default", subject: str = "", save: bool = False) -> dict:
    """Agent-facing 'do I like X?' — ledger first (free), LLM trait inference as fallback."""
    subject = subject.strip()
    if not subject:
        return JSONResponse({"error": "subject required"}, status_code=400)

    known = await affinity_engine.find_affinity(profile, subject)
    if known:
        return {
            "subject": subject, "known": True, "source": known["source"],
            "valence": known["valence"], "intensity": known["intensity"],
            "rationale": known["rationale"] or "",
            "verdict": _verdict(known["valence"]),
        }

    try:
        result = await _infer_opinion(profile, subject)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except json.JSONDecodeError:
        return JSONResponse({"error": "model did not return valid JSON"}, status_code=502)

    if save:
        await affinity_engine.apply_observation(
            profile, subject, valence=float(result["valence"]), intensity=float(result["intensity"]),
            confidence=0.5, category=result["category"], rationale=result["rationale"], source="observed",
        )
        result["saved"] = True

    result["known"] = False
    result["source"] = "inferred"
    result["verdict"] = _verdict(result["valence"])
    return result


def _verdict(valence: float) -> str:
    if valence >= 0.55:
        return "love"
    if valence >= 0.15:
        return "like"
    if valence > -0.15:
        return "neutral"
    if valence > -0.55:
        return "dislike"
    return "hate"
