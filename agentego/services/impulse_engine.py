"""Impulse engine — decides whether an idle agent takes a self-initiated action.

Per cron tick, eligible actions (mood gate + idle requirement met) enter a weighted
lottery against a "do nothing" ticket. Each action's weight = base_weight × recency
factor, so a recently fired action is less likely than other eligible ones but is
never disabled. Pacing comes from this recency weighting plus the cron cadence.
"""
import json
import time
import random
from uuid import uuid4
from ..db.ego import get_ego_db
from .settings_store import get_setting


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _row_to_action(r) -> dict:
    try:
        moods = json.loads(r[4]) if r[4] else []
    except Exception:
        moods = []
    return {
        "id": r[0], "profile_name": r[1], "label": r[2], "prompt": r[3],
        "required_moods": moods, "min_idle_minutes": r[5], "base_weight": r[6],
        "recency_window_minutes": r[7], "enabled": bool(r[8]),
        "last_fired_at": r[9], "created_at": r[10], "mood_negate": bool(r[11]),
    }


_ACTION_COLS = ("id, profile_name, label, prompt, required_moods, min_idle_minutes, "
                "base_weight, recency_window_minutes, enabled, last_fired_at, created_at, mood_negate")


# --- CRUD ---

async def list_actions(profile_name: str) -> list[dict]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            f"SELECT {_ACTION_COLS} FROM impulse_actions WHERE profile_name = ? ORDER BY created_at ASC",
            (profile_name,),
        )
        return [_row_to_action(r) for r in await cursor.fetchall()]
    finally:
        await conn.close()


async def get_action(action_id: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            f"SELECT {_ACTION_COLS} FROM impulse_actions WHERE id = ?", (action_id,)
        )
        row = await cursor.fetchone()
        return _row_to_action(row) if row else None
    finally:
        await conn.close()


async def create_action(profile_name: str, label: str, prompt: str, required_moods: list,
                        min_idle_minutes: int, base_weight: float, recency_window_minutes: int,
                        mood_negate: bool = False) -> str:
    aid = str(uuid4())
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO impulse_actions (id, profile_name, label, prompt, required_moods, "
            "min_idle_minutes, base_weight, recency_window_minutes, enabled, created_at, mood_negate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (aid, profile_name, label, prompt, json.dumps(required_moods),
             max(0, min_idle_minutes), max(0.0, base_weight), max(1, recency_window_minutes),
             time.time(), 1 if mood_negate else 0),
        )
        await conn.commit()
    finally:
        await conn.close()
    return aid


async def update_action(action_id: str, label: str, prompt: str, required_moods: list,
                        min_idle_minutes: int, base_weight: float, recency_window_minutes: int,
                        mood_negate: bool = False) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE impulse_actions SET label=?, prompt=?, required_moods=?, min_idle_minutes=?, "
            "base_weight=?, recency_window_minutes=?, mood_negate=? WHERE id=?",
            (label, prompt, json.dumps(required_moods), max(0, min_idle_minutes),
             max(0.0, base_weight), max(1, recency_window_minutes), 1 if mood_negate else 0, action_id),
        )
        await conn.commit()
    finally:
        await conn.close()


async def delete_action(action_id: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM impulse_actions WHERE id = ?", (action_id,))
        await conn.commit()
    finally:
        await conn.close()


async def toggle_action(action_id: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE impulse_actions SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",
            (action_id,),
        )
        await conn.commit()
    finally:
        await conn.close()


# --- Recency / idle ---

async def get_last_activity_ts(profile_name: str, db_path: str | None = None) -> float | None:
    """Most recent conversation end_ts for the profile = 'last talked to user'."""
    from .conversations import sync_recent_conversations, get_recent_conversations
    try:
        await sync_recent_conversations(profile_name, db_path=db_path)
    except Exception:
        pass
    convs = await get_recent_conversations(profile_name, limit=1)
    return convs[0]["end_ts"] if convs else None


# --- Logging ---

async def _log_fire(profile_name: str, action: dict, prompt: str, mood_id, idle_minutes: float) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO impulse_log (id, profile_name, action_id, label, prompt, mood_id, idle_minutes, fired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), profile_name, action["id"], action["label"], prompt, mood_id, idle_minutes, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_recent_log(profile_name: str, limit: int = 15) -> list[dict]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT label, prompt, mood_id, idle_minutes, fired_at FROM impulse_log "
            "WHERE profile_name = ? ORDER BY fired_at DESC LIMIT ?",
            (profile_name, limit),
        )
        return [
            {"label": r[0], "prompt": r[1], "mood_id": r[2], "idle_minutes": r[3], "fired_at": r[4]}
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()


# --- Prompt building ---

TASTE_PLACEHOLDERS = ("{likes}", "{dislikes}", "{interests}", "{personality}")


class _SafeDict(dict):
    """Leave unknown {placeholders} intact instead of raising KeyError."""
    def __missing__(self, key):
        return "{" + key + "}"


def build_prompt(action: dict, mood: dict | None, idle_minutes: float, taste: dict | None = None) -> str:
    fmt = {
        "mood": (mood or {}).get("name", "neutral"),
        "idle_minutes": int(idle_minutes) if idle_minutes != float("inf") else 0,
        "likes": "", "dislikes": "", "interests": "", "personality": "",
    }
    if taste:
        fmt.update(likes=taste.get("likes", ""), dislikes=taste.get("dislikes", ""),
                   interests=taste.get("interests", ""), personality=taste.get("personality", ""))
    try:
        return action["prompt"].format_map(_SafeDict(fmt))
    except Exception:
        return action["prompt"]


# --- The engine ---

async def evaluate_impulse(profile_name: str, db_path: str | None = None, commit: bool = True) -> dict:
    """Run the weighted lottery for a profile. commit=False = dry-run (no mutation)."""
    enabled_global = (await get_setting("impulse_enabled", "1")) == "1"
    try:
        restraint = float(await get_setting("impulse_restraint_weight", "0.5"))
    except (TypeError, ValueError):
        restraint = 0.5

    from .mood_engine import evaluate_mood
    mood = await evaluate_mood(profile_name, db_path=db_path)
    mood_id = mood["id"] if mood else None

    last_ts = await get_last_activity_ts(profile_name, db_path=db_path)
    idle_minutes = (time.time() - last_ts) / 60.0 if last_ts else float("inf")
    idle_out = None if idle_minutes == float("inf") else round(idle_minutes, 1)

    actions = await list_actions(profile_name)
    now = time.time()

    eligible = []
    for a in actions:
        if not a["enabled"]:
            continue
        if a["required_moods"]:
            in_list = mood_id in a["required_moods"]
            # negate=True means "any mood EXCEPT these"; otherwise "one of these"
            if a.get("mood_negate"):
                if in_list:
                    continue
            elif not in_list:
                continue
        if idle_minutes < a["min_idle_minutes"]:
            continue
        if a["last_fired_at"]:
            window = max(1, a["recency_window_minutes"]) * 60.0
            recency_factor = _clamp((now - a["last_fired_at"]) / window, 0.05, 1.0)
        else:
            recency_factor = 1.0
        weight = max(0.0, a["base_weight"]) * recency_factor
        eligible.append({**a, "recency_factor": round(recency_factor, 3), "weight": round(weight, 4)})

    total = sum(e["weight"] for e in eligible) + max(0.0, restraint)
    for e in eligible:
        e["probability"] = round(e["weight"] / total, 3) if total > 0 else 0.0
    nothing_prob = round(max(0.0, restraint) / total, 3) if total > 0 else 1.0

    result = {
        "profile": profile_name,
        "global_enabled": enabled_global,
        "mood": mood,
        "idle_minutes": idle_out,
        "eligible": eligible,
        "restraint_weight": restraint,
        "nothing_probability": nothing_prob,
        "fired": False,
        "action": None,
        "prompt": None,
    }

    if not enabled_global or not eligible or total <= 0:
        return result

    # Weighted draw over eligible actions + a "do nothing" ticket.
    pick = random.uniform(0, total)
    cumulative = 0.0
    chosen = None
    for e in eligible:
        cumulative += e["weight"]
        if pick <= cumulative:
            chosen = e
            break
    # if pick fell into the restraint band, chosen stays None (do nothing)

    if chosen is None:
        return result

    # Inject the agent's tastes only when the prompt references them.
    taste = None
    if any(ph in chosen["prompt"] for ph in TASTE_PLACEHOLDERS):
        from . import affinity_engine
        taste = await affinity_engine.get_taste_context(profile_name, sample=True)
    prompt = build_prompt(chosen, mood, idle_minutes, taste)
    result["fired"] = True
    result["action"] = {"id": chosen["id"], "label": chosen["label"]}
    result["prompt"] = prompt

    if commit:
        conn = await get_ego_db()
        try:
            await conn.execute(
                "UPDATE impulse_actions SET last_fired_at = ? WHERE id = ?", (now, chosen["id"])
            )
            await conn.commit()
        finally:
            await conn.close()
        await _log_fire(profile_name, chosen, prompt, mood_id, idle_out or 0.0)

    return result
