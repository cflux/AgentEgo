import json
import time
import random
from ..db.ego import get_ego_db

_LOOKBACK_MAX = 20


async def _load_defaults(profile_name: str, moods: dict) -> list:
    """Mood ids configured as this profile's resting-mood set (existing moods only)."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT mood_id FROM mood_defaults WHERE profile_name = ?", (profile_name,)
        )
        return [r[0] for r in await cursor.fetchall() if r[0] in moods]
    finally:
        await conn.close()


async def _load_moods() -> dict:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, name, color, icon, min_votes FROM moods ORDER BY name"
        )
        return {
            r[0]: {"id": r[0], "name": r[1], "color": r[2], "icon": r[3], "min_votes": r[4]}
            for r in await cursor.fetchall()
        }
    finally:
        await conn.close()


async def _load_rules(profile_name: str) -> list:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT id, mood_id, rule_type, params, label, mood_gate FROM mood_rules "
            "WHERE profile_name = ? AND enabled = 1",
            (profile_name,),
        )
        return [
            {
                "id": r[0], "mood_id": r[1], "rule_type": r[2],
                "params": json.loads(r[3]), "label": r[4], "mood_gate": r[5],
            }
            for r in await cursor.fetchall()
        ]
    finally:
        await conn.close()


async def _load_thresholds(profile_name: str) -> dict:
    """Returns {mood_id: min_votes} of per-profile overrides."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT mood_id, min_votes FROM mood_thresholds WHERE profile_name = ?",
            (profile_name,),
        )
        return {r[0]: r[1] for r in await cursor.fetchall()}
    finally:
        await conn.close()


async def _load_cached_mood(profile_name: str) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT mood_id FROM agent_moods WHERE profile_name = ?",
            (profile_name,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def _fetch_enrichment(session_ids: list) -> tuple[dict, dict, dict]:
    if not session_ids:
        return {}, {}, {}
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(session_ids))
        sentiment_map: dict = {}
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='sentiment' AND key IN ({ph})",
            session_ids,
        )
        for row in await cursor.fetchall():
            try:
                sentiment_map[row[0]] = json.loads(row[1])
            except Exception:
                pass
        topic_map: dict = {}
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='topic' AND key IN ({ph})",
            session_ids,
        )
        for row in await cursor.fetchall():
            topic_map[row[0]] = row[1]
        mode_map: dict = {}
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='mode' AND key IN ({ph})",
            session_ids,
        )
        for row in await cursor.fetchall():
            mode_map[row[0]] = row[1]
        return sentiment_map, topic_map, mode_map
    finally:
        await conn.close()


def _rule_fires(rule: dict, enriched: list) -> bool:
    p = rule["params"]
    rt = rule["rule_type"]

    if rt == "mode_streak":
        target = p.get("mode", "")
        count = max(1, int(p.get("count", 3)))
        negate = bool(p.get("negate", False))
        window = enriched[:count]
        if len(window) < count:
            return False
        return all((s.get("mode") != target) if negate else (s.get("mode") == target) for s in window)

    elif rt == "mode_count":
        target = p.get("mode", "")
        min_count = max(1, int(p.get("min_count", 2)))
        lookback = max(1, int(p.get("lookback", 5)))
        negate = bool(p.get("negate", False))
        matches = sum(
            1 for s in enriched[:lookback]
            if (s.get("mode") != target if negate else s.get("mode") == target)
        )
        return matches >= min_count

    elif rt == "sentiment_user":
        emotions = set(p.get("emotions", []))
        lookback = max(1, int(p.get("lookback", 1)))
        min_count = max(1, int(p.get("min_count", 1)))
        return sum(1 for s in enriched[:lookback] if s.get("sentiment_user") in emotions) >= min_count

    elif rt == "sentiment_agent":
        emotions = set(p.get("emotions", []))
        lookback = max(1, int(p.get("lookback", 1)))
        min_count = max(1, int(p.get("min_count", 1)))
        return sum(1 for s in enriched[:lookback] if s.get("sentiment_agent") in emotions) >= min_count

    elif rt == "sentiment_mismatch":
        emotions = set(p.get("emotions", []))
        direction = p.get("direction", "either")
        lookback = max(1, int(p.get("lookback", 1)))
        min_count = max(1, int(p.get("min_count", 1)))

        def _mismatches(s: dict) -> bool:
            u3 = set(s.get("sentiment_user_top3") or [])
            a3 = set(s.get("sentiment_agent_top3") or [])
            if direction == "user_only":
                return bool(emotions & (u3 - a3))
            elif direction == "agent_only":
                return bool(emotions & (a3 - u3))
            else:  # either
                return bool(emotions & (u3 - a3)) or bool(emotions & (a3 - u3))

        return sum(1 for s in enriched[:lookback] if _mismatches(s)) >= min_count

    elif rt == "topic_keyword":
        keywords = [k.lower() for k in p.get("keywords", [])]
        lookback = max(1, int(p.get("lookback", 5)))
        min_count = max(1, int(p.get("min_count", 1)))
        if not keywords:
            return False
        return sum(
            1 for s in enriched[:lookback]
            if s.get("topic") and any(kw in s["topic"].lower() for kw in keywords)
        ) >= min_count

    return False


async def _cache_result(profile_name: str, mood_id, votes: int, breakdown: list) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO agent_moods (profile_name, mood_id, vote_count, computed_at, breakdown)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
                mood_id = excluded.mood_id,
                vote_count = excluded.vote_count,
                computed_at = excluded.computed_at,
                breakdown = excluded.breakdown
            """,
            (profile_name, mood_id, votes, time.time(), json.dumps(breakdown)),
        )
        await conn.commit()
    finally:
        await conn.close()


async def evaluate_mood(profile_name: str, db_path: str | None = None) -> dict | None:
    """
    Evaluate mood rules for a profile using threshold voting.
    Returns {id, name, color, icon, vote_count, breakdown} or None.
    Caches the result in agent_moods.
    """
    moods = await _load_moods()
    rules = await _load_rules(profile_name)

    if not rules or not moods:
        await _cache_result(profile_name, None, 0, [])
        return None

    thresholds = await _load_thresholds(profile_name)
    cached_mood_id = await _load_cached_mood(profile_name)

    from .conversations import sync_recent_conversations, get_recent_conversations
    await sync_recent_conversations(profile_name, db_path=db_path)
    conversations = await get_recent_conversations(profile_name, limit=_LOOKBACK_MAX)
    if not conversations:
        await _cache_result(profile_name, None, 0, [])
        return None

    conv_ids = [c["id"] for c in conversations]
    sentiment_map, topic_map, mode_map = await _fetch_enrichment(conv_ids)

    enriched = []
    for c in conversations:
        cid = c["id"]
        sdata = sentiment_map.get(cid, {})
        user_data = sdata.get("user", {}) if sdata else {}
        agent_data = sdata.get("agent", {}) if sdata else {}
        enriched.append({
            "id": cid,
            "mode": mode_map.get(cid),
            "topic": topic_map.get(cid),
            "sentiment_user": user_data.get("dominant"),
            "sentiment_agent": agent_data.get("dominant"),
            "sentiment_user_top3": user_data.get("top3") or [],
            "sentiment_agent_top3": agent_data.get("top3") or [],
        })

    vote_map: dict[str, int] = {}
    breakdown: list[str] = []
    for rule in rules:
        if rule["mood_id"] not in moods:
            continue
        # Mood gate: skip rule if current cached mood doesn't match
        if rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id:
            continue
        if _rule_fires(rule, enriched):
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + 1
            label = rule.get("label") or _rule_label(rule)
            breakdown.append(label)

    def _threshold(mid: str) -> int:
        return thresholds.get(mid, moods[mid]["min_votes"])

    candidates = [
        (mid, votes)
        for mid, votes in vote_map.items()
        if votes >= _threshold(mid)
    ]

    if not candidates:
        # No rule won — fall back to the profile's default mood set, if any.
        defaults = await _load_defaults(profile_name, moods)
        if not defaults:
            await _cache_result(profile_name, None, 0, [])
            return None
        # Stable random: keep the current default if it's still a default, else pick anew.
        chosen = cached_mood_id if cached_mood_id in defaults else random.choice(defaults)
        await _cache_result(profile_name, chosen, 0, ["Default mood"])
        return {**moods[chosen], "vote_count": 0, "breakdown": ["Default mood"], "is_default": True}

    winner_id, winner_votes = max(candidates, key=lambda x: (x[1], _threshold(x[0])))
    winner = {**moods[winner_id], "vote_count": winner_votes, "breakdown": breakdown}
    await _cache_result(profile_name, winner_id, winner_votes, breakdown)
    return winner


async def explain_mood(profile_name: str, db_path: str | None = None) -> dict:
    """Read-only breakdown of the current mood computation, for debugging:
    the recent conversations + enrichment, which rules fired, and the vote tally."""
    moods = await _load_moods()
    rules = await _load_rules(profile_name)
    thresholds = await _load_thresholds(profile_name)
    cached_mood_id = await _load_cached_mood(profile_name)

    from .conversations import sync_recent_conversations, get_recent_conversations
    await sync_recent_conversations(profile_name, db_path=db_path)
    conversations = await get_recent_conversations(profile_name, limit=_LOOKBACK_MAX)
    conv_ids = [c["id"] for c in conversations]
    sentiment_map, topic_map, mode_map = await _fetch_enrichment(conv_ids)

    enriched = []
    for c in conversations:
        cid = c["id"]
        sdata = sentiment_map.get(cid, {})
        u = sdata.get("user", {}) if sdata else {}
        a = sdata.get("agent", {}) if sdata else {}
        enriched.append({
            "id": cid, "title": c.get("title"), "end_ts": c.get("end_ts"),
            "mode": mode_map.get(cid), "topic": topic_map.get(cid),
            "sentiment_user": u.get("dominant"), "sentiment_agent": a.get("dominant"),
            "sentiment_user_top3": u.get("top3") or [], "sentiment_agent_top3": a.get("top3") or [],
        })

    def _threshold(mid: str) -> int:
        return thresholds.get(mid, moods[mid]["min_votes"] if mid in moods else 1)

    vote_map: dict[str, int] = {}
    rule_results = []
    for rule in rules:
        in_catalog = rule["mood_id"] in moods
        gated = bool(rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id)
        fired = in_catalog and not gated and _rule_fires(rule, enriched)
        if fired:
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + 1
        rule_results.append({
            "label": rule.get("label") or _rule_label(rule),
            "mood_id": rule["mood_id"], "rule_type": rule["rule_type"],
            "gated": gated, "mood_gate": rule.get("mood_gate"), "fired": fired,
        })

    tally = []
    for mid, votes in sorted(vote_map.items(), key=lambda x: -x[1]):
        th = _threshold(mid)
        tally.append({
            "mood_id": mid, "name": moods[mid]["name"] if mid in moods else mid,
            "votes": votes, "threshold": th, "meets": votes >= th,
        })

    candidates = [(mid, v) for mid, v in vote_map.items() if v >= _threshold(mid)]
    winner = None
    is_default = False
    if candidates:
        wid, wv = max(candidates, key=lambda x: (x[1], _threshold(x[0])))
        winner = {"id": wid, "name": moods[wid]["name"], "votes": wv}
    else:
        defaults = await _load_defaults(profile_name, moods)
        if defaults:
            chosen = cached_mood_id if cached_mood_id in defaults else defaults[0]
            winner = {"id": chosen, "name": moods[chosen]["name"], "votes": 0}
            is_default = True

    return {
        "enriched": enriched[:12],
        "rules": rule_results,
        "tally": tally,
        "winner": winner,
        "is_default": is_default,
        "default_set": [moods[m]["name"] for m in await _load_defaults(profile_name, moods)],
        "cached_mood": cached_mood_id,
        "conversation_count": len(conversations),
    }


def _rule_label(rule: dict) -> str:
    p = rule["params"]
    rt = rule["rule_type"]
    gate = f"[while {rule['mood_gate']}] " if rule.get("mood_gate") else ""
    if rt == "mode_streak":
        op = "not in" if p.get("negate") else "all in"
        return f"{gate}Last {p.get('count',3)} sessions {op} {p.get('mode','?')} mode"
    elif rt == "mode_count":
        op = "not in" if p.get("negate") else "in"
        return f"{gate}{p.get('min_count',2)}+ of last {p.get('lookback',5)} sessions {op} {p.get('mode','?')} mode"
    elif rt == "sentiment_user":
        emo = ", ".join(p.get("emotions", [])[:3])
        return f"{gate}User felt {emo} recently"
    elif rt == "sentiment_agent":
        emo = ", ".join(p.get("emotions", [])[:3])
        return f"{gate}Agent expressed {emo} recently"
    elif rt == "sentiment_mismatch":
        emo = ", ".join(p.get("emotions", [])[:3])
        dir_map = {"user_only": "user/not agent", "agent_only": "agent/not user"}
        direction = dir_map.get(p.get("direction", "either"), "either direction")
        return f"{gate}Mismatch ({emo}) — {direction}"
    elif rt == "topic_keyword":
        kw = ", ".join(p.get("keywords", [])[:3])
        return f"{gate}Topic contained '{kw}'"
    return rt
