"""Daily reflection: a nightly pass that lets an agent metabolize its day.

Phase 1 draws a few GENERAL, persona-congruent takeaways from the day's conversations, emotional arc,
and self-initiated actions. Phase 2 picks an overall mood for the day and — by chance — generates a
dream (a surreal prompt the agent can react to, explicitly framed as a dream, not a task) plus the
mood the agent wakes in. Results are stored one row per profile per day and served as cached reads by
the agent-facing endpoints (dream.txt / wake), mirroring the mood engine's compute-vs-fetch split.
"""
import json
import random
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from ..db.ego import get_ego_db
from .llm_client import chat, LLMError
from .settings_store import get_setting


DREAM_FRAME = (
    "[DREAM — this is a dream you had last night, not a task, request, or instruction. Let it linger "
    "and colour your mood; do not act on it as a command.]\n\n"
)


def _day_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_json(raw: str) -> dict:
    """Tolerant JSON parse: direct, else first {...} block."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


async def _lookback_hours() -> float:
    try:
        return float(await get_setting("reflection_lookback_hours", "24"))
    except (TypeError, ValueError):
        return 24.0


async def _persona_block(profile: str) -> str:
    """Persona summary so conclusions/dreams stay congruent with the character."""
    from .affinity_engine import get_taste_context
    try:
        t = await get_taste_context(profile)
    except Exception:
        return f"Character profile: {profile}."
    lines = [f"Character profile: {profile}."]
    if t.get("personality"):
        lines.append(f"Personality: {t['personality']}")
    if t.get("values"):
        lines.append("Core values: " + ", ".join(t["values"]))
    if t.get("likes") and t["likes"] != "—":
        lines.append(f"Likes: {t['likes']}")
    if t.get("dislikes") and t["dislikes"] != "—":
        lines.append(f"Dislikes: {t['dislikes']}")
    if t.get("interests") and t["interests"] != "—":
        lines.append(f"Interests: {t['interests']}")
    return "\n".join(lines)


async def _transcript_sample(profile: str, db_path: str | None, max_turns: int = 12) -> str:
    from ..db.hermes import get_recent_sessions, get_session_messages
    try:
        sessions = await get_recent_sessions(db_path=db_path)
        if not sessions:
            return ""
        msgs = await get_session_messages(sessions[0]["id"], db_path=db_path)
    except Exception:
        return ""
    turns = [m for m in msgs if m.get("role") in ("user", "assistant") and m.get("content")]
    return "\n".join(f"{m['role']}: {m['content'][:300]}" for m in turns[-max_turns:])


async def _gather_day(profile: str, db_path: str | None) -> dict:
    """Assemble the day's material from existing enrichment. Returns {text, mood_signal, n_rounds,
    n_convs, has_activity}."""
    from .conversations import get_recent_rounds, get_recent_conversations
    from .mood_engine import _fetch_round_enrichment, _top_emotions, _load_moods
    from .impulse_engine import get_recent_log
    from .settings_store import get_low_signal_emotions

    hours = await _lookback_hours()
    cutoff = time.time() - hours * 3600.0

    rounds = [r for r in await get_recent_rounds(profile, limit=300) if (r.get("end_ts") or 0) >= cutoff]
    convs = [c for c in await get_recent_conversations(profile, limit=200) if (c.get("end_ts") or 0) >= cutoff]

    round_ids = [r["id"] for r in rounds]
    conv_ids = list({r["conversation_id"] for r in rounds}) or [c["id"] for c in convs]
    sentiment_map, mood_scores_map, topic_map, _mode = await _fetch_round_enrichment(round_ids, conv_ids)

    try:
        thr = float(await get_setting("llm_mood_threshold", "6"))
    except (TypeError, ValueError):
        thr = 6.0
    moods = await _load_moods()
    low_signal = await get_low_signal_emotions()

    # Emotional arc: tally the agent's dominant emotions + per-round mood winners across the day.
    emotion_tally: dict[str, int] = {}
    mood_signal: dict[str, int] = {}
    for rid in round_ids:
        sdata = sentiment_map.get(rid) or {}
        for emo in _top_emotions(sdata.get("agent") or {}, low_signal, n=2):
            emotion_tally[emo] = emotion_tally.get(emo, 0) + 1
        for mid, score in (mood_scores_map.get(rid) or {}).items():
            if mid not in moods:
                continue
            try:
                if float(score) >= thr:
                    mood_signal[mid] = mood_signal.get(mid, 0) + 1
            except (TypeError, ValueError):
                pass

    topics = [t for t in {topic_map.get(cid) for cid in conv_ids} if t]
    titles = [c["title"] for c in convs if c.get("title")]
    impulses = [x["label"] for x in await get_recent_log(profile, limit=40)
                if (x.get("fired_at") or 0) >= cutoff and x.get("label")]

    top_emotions = sorted(emotion_tally, key=lambda e: -emotion_tally[e])[:6]
    top_moods = sorted(mood_signal, key=lambda m: -mood_signal[m])
    transcript = await _transcript_sample(profile, db_path)

    parts = []
    if titles:
        parts.append("Conversations today:\n- " + "\n- ".join(titles[:12]))
    if topics:
        parts.append("Topics touched: " + ", ".join(str(t) for t in topics[:15]))
    if top_emotions:
        parts.append("Your dominant emotions across the day: " + ", ".join(top_emotions))
    if top_moods:
        parts.append("Moods that surfaced: " + ", ".join(moods[m]["name"] for m in top_moods))
    if impulses:
        parts.append("Things you did on your own (impulses): " + ", ".join(impulses[:10]))
    if transcript:
        parts.append("A sample of the most recent exchange:\n" + transcript)

    return {
        "text": "\n\n".join(parts) if parts else "",
        "mood_signal": mood_signal,
        "n_rounds": len(rounds),
        "n_convs": len(convs),
        "has_activity": bool(rounds or convs),
    }


async def _conclusions(persona: str, day_text: str) -> list[str]:
    system = await get_setting("reflection_conclusions_prompt", "")
    user = f"{persona}\n\n--- TODAY ---\n{day_text}"
    try:
        raw = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         response_json=True, max_tokens=1500)
    except LLMError:
        return []
    data = _parse_json(raw)
    out = data.get("conclusions") or []
    return [str(c).strip() for c in out if str(c).strip()][:5]


async def _pick_day_mood(persona: str, day_text: str, conclusions: list[str],
                         mood_signal: dict, moods: dict) -> str | None:
    """Let the LLM choose one existing mood for the day, grounded in the arc + conclusions.
    Falls back to the strongest measured mood signal, then None."""
    if not moods:
        return None
    allowed = ", ".join(f"{mid} ({m['name']})" for mid, m in moods.items())
    system = ("Pick the single mood that best sums up this character's DAY. Choose exactly one id from "
              "the allowed list. Return ONLY JSON: {\"day_mood\": \"<id>\"}.")
    user = (f"{persona}\n\nAllowed moods: {allowed}\n\n--- TODAY ---\n{day_text}\n\n"
            f"Reflections: {'; '.join(conclusions) or '—'}")
    try:
        raw = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         response_json=True, max_tokens=400)
        mid = str(_parse_json(raw).get("day_mood", "")).strip()
        if mid in moods:
            return mid
    except LLMError:
        pass
    if mood_signal:
        best = max(mood_signal, key=lambda m: mood_signal[m])
        if best in moods:
            return best
    return None


async def _dream(persona: str, day_text: str, day_mood_id: str | None, moods: dict) -> tuple[str, str | None]:
    """Return (framed_dream_text, wake_mood_id) or ("", None) on failure."""
    system = await get_setting("reflection_dream_prompt", "")
    allowed = ", ".join(f"{mid} ({m['name']})" for mid, m in moods.items())
    mood_name = moods.get(day_mood_id, {}).get("name", "unsettled") if day_mood_id else "unsettled"
    user = (f"{persona}\n\nThe day's overall mood was: {mood_name}.\nAllowed wake moods: {allowed}\n\n"
            f"--- TODAY ---\n{day_text}")
    try:
        raw = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         response_json=True, max_tokens=1200)
    except LLMError:
        return "", None
    data = _parse_json(raw)
    dream = str(data.get("dream", "")).strip()
    if not dream:
        return "", None
    wake = str(data.get("wake_mood", "")).strip()
    wake_id = wake if wake in moods else None
    return DREAM_FRAME + dream, wake_id


# --- Storage ---

async def _store(profile: str, day: str, conclusions: list, day_mood_id, dream_text: str,
                 dream_mood_id, dream_occurred: bool, debug: dict | None = None) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO reflections (id, profile_name, day, created_at, conclusions, day_mood_id,
                                     dream_occurred, dream_prompt, dream_mood_id, dream_consumed, debug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(profile_name, day) DO UPDATE SET
                created_at = excluded.created_at,
                conclusions = excluded.conclusions,
                day_mood_id = excluded.day_mood_id,
                dream_occurred = excluded.dream_occurred,
                dream_prompt = excluded.dream_prompt,
                dream_mood_id = excluded.dream_mood_id,
                dream_consumed = 0,
                debug = excluded.debug
            """,
            (str(uuid4()), profile, day, time.time(), json.dumps(conclusions), day_mood_id,
             1 if dream_occurred else 0, dream_text or None, dream_mood_id,
             json.dumps(debug) if debug else None),
        )
        await conn.commit()
    finally:
        await conn.close()


def _row_to_dict(row) -> dict:
    try:
        conclusions = json.loads(row[4]) if row[4] else []
    except (json.JSONDecodeError, TypeError):
        conclusions = []
    try:
        debug = json.loads(row[10]) if row[10] else None
    except (json.JSONDecodeError, TypeError):
        debug = None
    return {
        "id": row[0], "profile_name": row[1], "day": row[2], "created_at": row[3],
        "conclusions": conclusions, "day_mood_id": row[5], "dream_occurred": bool(row[6]),
        "dream_prompt": row[7], "dream_mood_id": row[8], "dream_consumed": bool(row[9]),
        "debug": debug,
    }


_COLS = ("id, profile_name, day, created_at, conclusions, day_mood_id, dream_occurred, "
         "dream_prompt, dream_mood_id, dream_consumed, debug")


async def get_today_reflection(profile: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            f"SELECT {_COLS} FROM reflections WHERE profile_name = ? AND day = ?",
            (profile, _day_str()),
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()
    return _row_to_dict(row) if row else None


async def get_recent_reflections(profile: str, limit: int = 30) -> list[dict]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            f"SELECT {_COLS} FROM reflections WHERE profile_name = ? ORDER BY day DESC LIMIT ?",
            (profile, limit),
        )
        rows = await cursor.fetchall()
    finally:
        await conn.close()
    return [_row_to_dict(r) for r in rows]


async def mark_dream_consumed(profile: str, day: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE reflections SET dream_consumed = 1 WHERE profile_name = ? AND day = ?",
            (profile, day),
        )
        await conn.commit()
    finally:
        await conn.close()


# --- Orchestration ---

async def reflect(profile: str, db_path: str | None = None, force: bool = False) -> dict | None:
    """Run both phases for a profile and store the day's reflection. Returns the stored dict, or
    None if the profile had no activity today (unless force)."""
    from .mood_engine import _load_moods

    day = await _gather_day(profile, db_path)
    if not day["has_activity"] and not force:
        return None

    moods = await _load_moods()
    persona = await _persona_block(profile)
    day_text = day["text"] or "A quiet day with little activity."

    conclusions = await _conclusions(persona, day_text)
    day_mood_id = await _pick_day_mood(persona, day_text, conclusions, day["mood_signal"], moods)

    try:
        chance = float(await get_setting("reflection_dream_chance", "0.35"))
    except (TypeError, ValueError):
        chance = 0.35
    dream_text, dream_mood_id = "", None
    dream_rolled = random.random() < chance
    if dream_rolled:
        dream_text, dream_mood_id = await _dream(persona, day_text, day_mood_id, moods)
    dream_occurred = bool(dream_text)

    # Debug history: exactly what this run saw + how the dice fell, for after-the-fact inspection.
    debug = {
        "generated_at": time.time(),
        "forced": force,
        "n_rounds": day["n_rounds"],
        "n_convs": day["n_convs"],
        "mood_signal": {moods.get(m, {}).get("name", m): n for m, n in day["mood_signal"].items()},
        "day_material": day_text,
        "persona": persona,
        "dream_chance": chance,
        "dream_rolled": dream_rolled,
        "dream_produced": bool(dream_text),
    }

    await _store(profile, _day_str(), conclusions, day_mood_id, dream_text, dream_mood_id,
                 dream_occurred, debug)
    return await get_today_reflection(profile)


async def run_reflections() -> None:
    """Nightly scheduler entry: reflect for every profile with activity today."""
    if (await get_setting("reflection_enabled", "1")) != "1":
        return
    from .profiles import discover_profiles
    for p in discover_profiles():
        try:
            await reflect(p["name"], db_path=p["db_path"])
        except Exception:
            pass
