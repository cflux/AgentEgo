"""Intrusive thoughts — an optional short prompt that piggybacks on the mood directive on some turns.

Thematically: a thought the agent didn't choose, surfacing and passing. The mood directive endpoint is
hit per-turn and is a cheap pure read, so selection is done in memory. The one exception is the Loose
Threads source: when it actually fires it records the surfaced item to an anti-repeat ledger (a bounded
write, only on those rare turns). Every fetch re-rolls, so a thought flickers in and out.

Model:
- A per-profile catalog of thoughts (`intrusive_thoughts` table). Each has a base `weight` and a
  tri-state per-mood association (`mood_assoc` = {mood_id: "positive" | "negative"}, neutral omitted).
- A global gate probability (the "overall weight") decides whether *any* intrusion fires this turn.
- When one fires, a thought is picked weighted by base_weight × (pos/neg/neutral multiplier for the
  current mood). Positive-marked moods boost it; negative-marked dampen it.
- A special `kind='loose_threads'` entry carries no fixed text; if chosen, it injects a random
  "loose thread" pulled live from the agent's Den (THE_THREAD.md). See `den.get_loose_threads`.

CRUD + selection live here; the router/templates render the management page. LLM-free, deterministic
apart from the rolls.
"""
import hashlib
import json
import random
import time
from uuid import uuid4

from ..db.ego import get_ego_db
from . import den, settings_store

_COLS = "id, profile_name, kind, text, weight, mood_assoc, enabled, created_at"

VALID_KINDS = ("static", "loose_threads")

# Loose-thread anti-repeat ledger (module_data): once a loose thread surfaces it's disqualified for a
# window; entries whose item is no longer in the Den's pool (overwritten) are pruned on next resolve.
_LOOSE_SEEN_MODULE = "intrusive_loose_seen"


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _row(r) -> dict:
    return {
        "id": r[0], "profile_name": r[1], "kind": r[2], "text": r[3],
        "weight": r[4], "mood_assoc": _j(r[5], {}), "enabled": bool(r[6]), "created_at": r[7],
    }


# --- CRUD ---

async def list_thoughts(profile_name: str, enabled_only: bool = False) -> list[dict]:
    conn = await get_ego_db()
    try:
        q = f"SELECT {_COLS} FROM intrusive_thoughts WHERE profile_name = ?"
        if enabled_only:
            q += " AND enabled = 1"
        q += " ORDER BY created_at ASC"
        cur = await conn.execute(q, (profile_name,))
        return [_row(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_thought(tid: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(f"SELECT {_COLS} FROM intrusive_thoughts WHERE id = ?", (tid,))
        r = await cur.fetchone()
        return _row(r) if r else None
    finally:
        await conn.close()


async def has_loose_threads_row(profile_name: str) -> bool:
    """Whether this profile already has the single Loose Threads source (kept to one per profile)."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT 1 FROM intrusive_thoughts WHERE profile_name = ? AND kind = 'loose_threads' LIMIT 1",
            (profile_name,),
        )
        return (await cur.fetchone()) is not None
    finally:
        await conn.close()


async def create_thought(profile_name: str, *, kind: str = "static", text: str = "",
                         weight: float = 1.0, mood_assoc: dict | None = None,
                         enabled: bool = True) -> str:
    kind = kind if kind in VALID_KINDS else "static"
    tid = str(uuid4())
    conn = await get_ego_db()
    try:
        await conn.execute(
            f"INSERT INTO intrusive_thoughts ({_COLS}) VALUES (?,?,?,?,?,?,?,?)",
            (tid, profile_name, kind, text.strip() if kind == "static" else "",
             max(0.0, float(weight)), json.dumps(_clean_assoc(mood_assoc)),
             1 if enabled else 0, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return tid


async def update_thought(tid: str, **fields) -> None:
    allowed = {"text", "weight", "mood_assoc", "enabled"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "mood_assoc":
            v = json.dumps(_clean_assoc(v))
        elif k == "enabled":
            v = 1 if v else 0
        elif k == "weight":
            v = max(0.0, float(v))
        elif k == "text":
            v = (v or "").strip()
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    conn = await get_ego_db()
    try:
        await conn.execute(f"UPDATE intrusive_thoughts SET {', '.join(sets)} WHERE id = ?", (*vals, tid))
        await conn.commit()
    finally:
        await conn.close()


async def delete_thought(tid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM intrusive_thoughts WHERE id = ?", (tid,))
        await conn.commit()
    finally:
        await conn.close()


async def toggle_thought(tid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE intrusive_thoughts SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?", (tid,))
        await conn.commit()
    finally:
        await conn.close()


def _clean_assoc(assoc: dict | None) -> dict:
    """Keep only {mood_id: 'positive'|'negative'} entries (neutral / unknown dropped)."""
    out = {}
    for mid, pol in (assoc or {}).items():
        if pol in ("positive", "negative") and mid:
            out[str(mid)] = pol
    return out


# --- Per-profile enable (module_data, defaults to on) ---

async def is_profile_enabled(profile_name: str) -> bool:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT value FROM module_data WHERE module = 'intrusive_enabled' AND key = ?", (profile_name,))
        row = await cur.fetchone()
    finally:
        await conn.close()
    return row is None or row[0] == "1"  # absent = enabled


async def set_profile_enabled(profile_name: str, on: bool) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('intrusive_enabled', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (profile_name, "1" if on else "0", time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


# --- Weighting + selection (pure; no DB writes) ---

def mood_multiplier(mood_assoc: dict, mood_id: str | None, pos_mult: float, neg_mult: float) -> float:
    """Global boost/dampen for a thought given the current mood's tri-state association."""
    pol = (mood_assoc or {}).get(mood_id) if mood_id else None
    if pol == "positive":
        return pos_mult
    if pol == "negative":
        return neg_mult
    return 1.0


def effective_weight(thought: dict, mood_id: str | None, pos_mult: float, neg_mult: float) -> float:
    return max(0.0, float(thought.get("weight", 0.0))) * mood_multiplier(
        thought.get("mood_assoc", {}), mood_id, pos_mult, neg_mult)


def _weighted_pick(items: list, weight_fn):
    """Pick one item weighted by weight_fn (floored so a dampened item can still rarely surface)."""
    weights = [max(1e-6, weight_fn(it)) for it in items]
    total = sum(weights)
    if total <= 0:
        return None
    r = random.uniform(0, total)
    cum = 0.0
    for it, w in zip(items, weights):
        cum += w
        if r <= cum:
            return it
    return items[-1]


async def resolve_thought_text(thought: dict, profile_name: str) -> str | None:
    """The text to inject for a chosen thought. Static → its text; loose_threads → a live, non-repeating
    loose thread from the Den (None if the source is empty or all items were recently surfaced)."""
    if thought.get("kind") == "loose_threads":
        return await pick_loose_thread(profile_name)
    text = (thought.get("text") or "").strip()
    return text or None


# --- Loose-thread anti-repeat ledger ---

def _loose_hash(text: str) -> str:
    """Stable (cross-restart) short hash of a loose-thread bullet, for the surfaced-item ledger."""
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:16]


async def _load_loose_seen(profile_name: str) -> dict:
    """{item_hash: last_surfaced_ts} for this profile."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT key, value FROM module_data WHERE module = ? AND key LIKE ?",
            (_LOOSE_SEEN_MODULE, f"{profile_name}:%"))
        rows = await cur.fetchall()
    finally:
        await conn.close()
    out = {}
    for k, v in rows:
        try:
            out[k.rsplit(":", 1)[-1]] = float(v)
        except (TypeError, ValueError):
            pass
    return out


async def _record_loose_seen(profile_name: str, item_hash: str, ts: float) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_LOOSE_SEEN_MODULE, f"{profile_name}:{item_hash}", str(ts), ts))
        await conn.commit()
    finally:
        await conn.close()


async def _prune_loose_seen(profile_name: str, hashes) -> None:
    """Drop ledger entries for items no longer in the pool (the thread block was overwritten)."""
    hashes = list(hashes)
    if not hashes:
        return
    conn = await get_ego_db()
    try:
        await conn.executemany(
            "DELETE FROM module_data WHERE module = ? AND key = ?",
            [(_LOOSE_SEEN_MODULE, f"{profile_name}:{h}") for h in hashes])
        await conn.commit()
    finally:
        await conn.close()


async def _loose_window_seconds() -> float:
    try:
        h = float(await settings_store.get_setting("intrusive_loose_repeat_window_hours", "168"))
    except (TypeError, ValueError):
        h = 168.0
    return max(0.0, h) * 3600.0


async def pick_loose_thread(profile_name: str) -> str | None:
    """Pick a loose thread from the Den, excluding ones surfaced within the repeat window. Prunes
    ledger entries for items no longer in the pool. Records the pick (a write). None if the pool is
    empty or every item was recently surfaced."""
    label = await settings_store.get_setting("intrusive_loose_thread_label", "Loose threads")
    items = den.get_loose_threads(profile_name, label=label)
    if not items:
        return None
    now = time.time()
    window = await _loose_window_seconds()
    by_hash = {_loose_hash(i): i for i in items}
    seen = await _load_loose_seen(profile_name)
    stale = [h for h in seen if h not in by_hash]     # overwritten → forget
    if stale:
        await _prune_loose_seen(profile_name, stale)
        for h in stale:
            seen.pop(h, None)
    eligible = [i for h, i in by_hash.items() if h not in seen or (now - seen[h]) >= window]
    if not eligible:
        return None
    chosen = random.choice(eligible)
    await _record_loose_seen(profile_name, _loose_hash(chosen), now)
    return chosen


async def loose_eligibility(profile_name: str) -> tuple[int, int]:
    """(total_in_pool, eligible_now) for the loose-thread source — read-only (no pruning/writes),
    for the management page so the anti-repeat state is visible."""
    label = await settings_store.get_setting("intrusive_loose_thread_label", "Loose threads")
    items = den.get_loose_threads(profile_name, label=label)
    if not items:
        return (0, 0)
    now = time.time()
    window = await _loose_window_seconds()
    seen = await _load_loose_seen(profile_name)
    elig = sum(1 for i in items
               if (h := _loose_hash(i)) not in seen or (now - seen[h]) >= window)
    return (len(items), elig)


async def pick_intrusive_thought(profile_name: str, mood_id: str | None) -> str | None:
    """Decide whether an intrusive thought fires this turn and, if so, return its text. Pure read.

    None when: disabled (global or per-profile), the gate roll fails, no eligible thoughts, or the
    chosen thought resolves to no text (e.g. an empty Loose Threads source)."""
    if (await settings_store.get_setting("intrusive_thoughts_enabled", "1")) != "1":
        return None
    if not await is_profile_enabled(profile_name):
        return None
    try:
        p = float(await settings_store.get_setting("intrusive_thought_probability", "0.15"))
    except (TypeError, ValueError):
        p = 0.15
    if random.random() >= max(0.0, min(1.0, p)):
        return None
    thoughts = await list_thoughts(profile_name, enabled_only=True)
    if not thoughts:
        return None
    pos_mult, neg_mult = await _multipliers()
    chosen = _weighted_pick(thoughts, lambda t: effective_weight(t, mood_id, pos_mult, neg_mult))
    if not chosen:
        return None
    return await resolve_thought_text(chosen, profile_name)


async def _multipliers() -> tuple[float, float]:
    async def _f(key, default):
        try:
            return float(await settings_store.get_setting(key, default))
        except (TypeError, ValueError):
            return float(default)
    return (await _f("intrusive_positive_multiplier", "2.5"),
            await _f("intrusive_negative_multiplier", "0.3"))
