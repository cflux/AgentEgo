"""Mood-rule presets: snapshot a profile's mood configuration (rules + per-mood vote thresholds +
resting-mood set) as a reusable named set, and apply it to another profile as a starting point.

Moods themselves and the cascade/adjacency/decay tuning are global (shared by all profiles), so a
preset only carries the per-profile pieces. Stored in module_data under module='mood_preset'.
"""
import json
import time
from uuid import uuid4

from ..db.ego import get_ego_db

PRESET_MODULE = "mood_preset"
DEFAULT_NAME = "default"


async def capture(profile_name: str) -> dict:
    """Snapshot a profile's mood config: rules (order preserved), threshold overrides, resting set."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT mood_id, rule_type, params, label, mood_gate, enabled FROM mood_rules "
            "WHERE profile_name = ? ORDER BY created_at ASC",
            (profile_name,),
        )
        rules = [
            {"mood_id": r[0], "rule_type": r[1], "params": r[2], "label": r[3],
             "mood_gate": r[4], "enabled": r[5]}
            for r in await cur.fetchall()
        ]
        cur = await conn.execute(
            "SELECT mood_id, min_votes FROM mood_thresholds WHERE profile_name = ?", (profile_name,)
        )
        thresholds = {r[0]: r[1] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT mood_id FROM mood_defaults WHERE profile_name = ?", (profile_name,)
        )
        defaults = [r[0] for r in await cur.fetchall()]
    finally:
        await conn.close()
    return {"rules": rules, "thresholds": thresholds, "defaults": defaults}


async def save_preset(profile_name: str, name: str = DEFAULT_NAME) -> dict:
    """Capture a profile's mood config and store it as a named preset."""
    payload = {**await capture(profile_name), "saved_from": profile_name, "saved_at": time.time()}
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (PRESET_MODULE, name, json.dumps(payload), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return payload


async def get_preset(name: str = DEFAULT_NAME) -> dict | None:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT value FROM module_data WHERE module = ? AND key = ?", (PRESET_MODULE, name)
        )
        row = await cur.fetchone()
    finally:
        await conn.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


async def apply_preset(profile_name: str, name: str = DEFAULT_NAME, replace: bool = True) -> int:
    """Apply a preset to a profile. replace=True clears the profile's existing rules/thresholds/
    resting set first (a clean starting point). Returns the rule count applied, or -1 if no preset."""
    preset = await get_preset(name)
    if not preset:
        return -1
    conn = await get_ego_db()
    try:
        if replace:
            await conn.execute("DELETE FROM mood_rules WHERE profile_name = ?", (profile_name,))
            await conn.execute("DELETE FROM mood_thresholds WHERE profile_name = ?", (profile_name,))
            await conn.execute("DELETE FROM mood_defaults WHERE profile_name = ?", (profile_name,))
        now = time.time()
        for i, r in enumerate(preset.get("rules", [])):
            await conn.execute(
                "INSERT INTO mood_rules (id, profile_name, mood_id, rule_type, params, label, "
                "mood_gate, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), profile_name, r["mood_id"], r["rule_type"], r["params"],
                 r.get("label"), r.get("mood_gate"), r.get("enabled", 1), now + i * 0.001),
            )
        for mood_id, mv in (preset.get("thresholds") or {}).items():
            await conn.execute(
                "INSERT INTO mood_thresholds (profile_name, mood_id, min_votes) VALUES (?, ?, ?) "
                "ON CONFLICT(profile_name, mood_id) DO UPDATE SET min_votes = excluded.min_votes",
                (profile_name, mood_id, mv),
            )
        for mood_id in preset.get("defaults", []):
            await conn.execute(
                "INSERT OR IGNORE INTO mood_defaults (profile_name, mood_id) VALUES (?, ?)",
                (profile_name, mood_id),
            )
        await conn.commit()
    finally:
        await conn.close()
    return len(preset.get("rules", []))
