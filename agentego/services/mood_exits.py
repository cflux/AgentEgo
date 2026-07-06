"""Mood exit triggers — directed, abrupt mood transitions (the twin of cascade).

"[While in source_mood,] if [condition] → resolve to target_mood." Two condition types:
  - llm     — a natural-language yes/no read of the last ≤2 rounds ("the afterglow feeling has appeared").
  - signal  — a deterministic `metric OP value` threshold on a session metric from the SIGNALS registry.

Soft exits (default) only fire to *unstick a stale source mood* (they defer to the backbone — if normal
scoring is already moving her to a third mood, that wins). Hard exits snap regardless (resource-depletion
overrides). On fire the mood being left is put on cooldown so the stale lookback can't bounce her back.

Evaluated AFTER normal resolution (see mood_engine.evaluate_mood) — it wraps the winner, never touches the
scoring internals.
"""
import json
import time
from uuid import uuid4
from ..db.ego import get_ego_db

_COLS = ("id, profile_name, source_mood, target_mood, condition_type, condition, hard, note, enabled, created_at")

# --- Signal registry (extensible: one @signal function per metric) ---
SIGNALS: dict[str, dict] = {}


def signal(name: str, label: str = "", unit: str = ""):
    def deco(fn):
        SIGNALS[name] = {"fn": fn, "label": label or name, "unit": unit}
        return fn
    return deco


@signal("message_count", "Messages in session", "msgs")
async def _s_msgs(profile, sess, ctx):
    return float(sess.get("message_count") or 0)


@signal("tool_call_count", "Tool calls in session", "calls")
async def _s_tools(profile, sess, ctx):
    return float(sess.get("tool_call_count") or 0)


@signal("api_call_count", "LLM calls in session", "calls")
async def _s_api(profile, sess, ctx):
    return float(sess.get("api_call_count") or 0)


@signal("total_tokens", "Tokens used", "tok")
async def _s_tokens(profile, sess, ctx):
    return float(sum(sess.get(k) or 0 for k in
                     ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")))


@signal("session_minutes", "Session length", "min")
async def _s_dur(profile, sess, ctx):
    st = sess.get("started_at") or 0
    return round((ctx["now"] - st) / 60.0, 1) if st else 0.0


@signal("idle_minutes", "Since last user message", "min")
async def _s_idle(profile, sess, ctx):
    from .impulse_engine import get_last_activity_ts
    from .profiles import resolve_profile
    ts = await get_last_activity_ts(profile, db_path=resolve_profile(profile))
    return round((ctx["now"] - ts) / 60.0, 1) if ts else 0.0


@signal("hour_of_day", "Hour of day (local 0–23)", "h")
async def _s_hour(profile, sess, ctx):
    import datetime
    return float(datetime.datetime.fromtimestamp(ctx["now"]).hour)


_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "==": lambda a, b: a == b}


# --- CRUD ---

def _row(r) -> dict:
    try:
        cond = json.loads(r[5]) if r[5] else {}
    except (TypeError, ValueError):
        cond = {}
    return {
        "id": r[0], "profile_name": r[1], "source_mood": r[2], "target_mood": r[3],
        "condition_type": r[4] or "llm", "condition": cond, "hard": bool(r[6]),
        "note": r[7], "enabled": bool(r[8]), "created_at": r[9],
    }


async def list_exits(profile_name: str, enabled_only: bool = False) -> list[dict]:
    conn = await get_ego_db()
    try:
        q = f"SELECT {_COLS} FROM mood_exits WHERE profile_name = ?"
        if enabled_only:
            q += " AND enabled = 1"
        q += " ORDER BY created_at ASC"
        return [_row(r) for r in await (await conn.execute(q, (profile_name,))).fetchall()]
    finally:
        await conn.close()


async def create_exit(profile_name: str, target_mood: str, condition_type: str, condition: dict, *,
                      source_mood: str | None = None, hard: bool = False, note: str = "",
                      enabled: bool = True) -> str:
    eid = str(uuid4())
    conn = await get_ego_db()
    try:
        await conn.execute(
            f"INSERT INTO mood_exits ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, profile_name, source_mood or None, target_mood,
             "signal" if condition_type == "signal" else "llm", json.dumps(condition or {}),
             1 if hard else 0, note, 1 if enabled else 0, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return eid


async def update_exit(eid: str, **fields) -> None:
    allowed = {"source_mood", "target_mood", "condition_type", "condition", "hard", "note", "enabled"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "condition":
            v = json.dumps(v)
        elif k in ("hard", "enabled"):
            v = 1 if v else 0
        elif k == "source_mood":
            v = v or None
        sets.append(f"{k} = ?"); vals.append(v)
    if not sets:
        return
    conn = await get_ego_db()
    try:
        await conn.execute(f"UPDATE mood_exits SET {', '.join(sets)} WHERE id = ?", (*vals, eid))
        await conn.commit()
    finally:
        await conn.close()


async def delete_exit(eid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM mood_exits WHERE id = ?", (eid,))
        await conn.commit()
    finally:
        await conn.close()


async def toggle_exit(eid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("UPDATE mood_exits SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?", (eid,))
        await conn.commit()
    finally:
        await conn.close()


# --- Session row (full metric columns) + transcript ---

async def _active_session(profile_name: str) -> dict:
    """The profile's most recent non-cron session, with all metric columns (for signal exits)."""
    import aiosqlite
    from .profiles import resolve_profile
    from ..db.hermes import cutoff_ts
    db = resolve_profile(profile_name)
    try:
        async with aiosqlite.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA query_only = true")
            cur = await conn.execute(
                "SELECT id, started_at, message_count, tool_call_count, api_call_count, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd "
                "FROM sessions WHERE started_at >= ? AND id NOT LIKE 'cron_%' ORDER BY started_at DESC LIMIT 1",
                (cutoff_ts(),))
            row = await cur.fetchone()
            return dict(row) if row else {}
    except Exception:
        return {}


async def _recent_transcript(profile_name: str, max_turns: int = 6) -> str:
    from .impulse_arbiter import recent_gist
    from .profiles import resolve_profile
    return await recent_gist(profile_name, resolve_profile(profile_name), max_turns=max_turns)


# --- LLM yes/no (remote llm_client — reachable from the container; gated + cached) ---

_YESNO_CACHE: dict = {}  # (exit_id, round_key) -> bool


async def _llm_yesno(condition_text: str, transcript: str) -> bool:
    from .llm_client import chat, LLMError
    if not condition_text or not transcript:
        return False
    # Neutral framing on purpose: an "AI companion / their emotional state" framing makes the remote
    # model hedge/refuse on the explicit content these conditions are often about (afterglow, etc.).
    system = ("You judge whether a described feeling or state is present in a short conversation excerpt. "
              "Answer with ONLY 'yes' or 'no'.")
    user = f"Recent exchange:\n{transcript}\n\nIs this true right now: \"{condition_text}\"?"
    try:
        out = (await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          temperature=0.0, max_tokens=64)).strip().lower()
    except LLMError:
        return False
    return out.startswith("y")


# --- Fire evaluation ---

async def evaluate_exits(profile_name: str, winner_id: str, cached_mood: str | None,
                         moods: dict, enriched: list) -> dict | None:
    """After normal resolution, decide whether an exit overrides the winner. Returns
    {target, from_mood, note} on the first firing exit, else None."""
    exits = await list_exits(profile_name, enabled_only=True)
    if not exits:
        return None
    now = time.time()
    round_key = enriched[0]["id"] if enriched else str(int(now // 60))
    sess = None
    transcript = None
    for ex in exits:
        src = ex["source_mood"]
        tgt = ex["target_mood"]
        if tgt not in moods or tgt == winner_id:
            continue
        if src:
            # Soft: gate on the resolved winner — fires only if she'd otherwise STAY in the source
            #       (so a genuine backbone transition to a third mood wins instead).
            # Hard: gate on the current incumbent — fires because she's IN the source, regardless of
            #       where scoring would move her.
            gate = winner_id if not ex["hard"] else (cached_mood or winner_id)
            if src != gate:
                continue
        fired, detail = False, ""
        if ex["condition_type"] == "signal":
            cond = ex["condition"]; metric = cond.get("metric"); op = cond.get("op")
            try:
                val = float(cond.get("value"))
            except (TypeError, ValueError):
                continue
            if metric in SIGNALS and op in _OPS:
                if sess is None:
                    sess = await _active_session(profile_name)
                cur = await SIGNALS[metric]["fn"](profile_name, sess or {}, {"now": now})
                if _OPS[op](cur, val):
                    fired, detail = True, f"{metric} {cur:g} {op} {val:g}"
        else:  # llm
            ckey = (ex["id"], round_key)
            if ckey in _YESNO_CACHE:
                fired = _YESNO_CACHE[ckey]
            else:
                if transcript is None:
                    transcript = await _recent_transcript(profile_name)
                fired = await _llm_yesno(ex["condition"].get("text", ""), transcript)
                if len(_YESNO_CACHE) > 200:
                    _YESNO_CACHE.clear()
                _YESNO_CACHE[ckey] = fired
            if fired:
                detail = (ex["condition"].get("text", "") or "")[:48]
        if fired:
            frm = src or winner_id   # cooldown the source (or the mood being overridden, for any-source)
            return {"target": tgt, "from_mood": frm,
                    "note": f"Exited {moods.get(frm, {}).get('name', frm)} → {moods[tgt]['name']} ({detail})"}
    return None
