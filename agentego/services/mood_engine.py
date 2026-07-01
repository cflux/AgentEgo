import json
import time
import random
from ..db.ego import get_ego_db

_LOOKBACK_MAX = 20  # fallback if the configurable setting is unavailable


async def _lookback_rounds() -> int:
    """How many recent rounds the mood engine evaluates (configurable)."""
    from .settings_store import get_setting
    try:
        return max(1, int(await get_setting("mood_lookback_rounds", str(_LOOKBACK_MAX))))
    except (TypeError, ValueError):
        return _LOOKBACK_MAX


# Fallback if the configurable setting is unavailable.
LOW_SIGNAL_EMOTIONS = {"neutral", "approval"}


def _top_emotions(party: dict, low_signal: set | None = None, n: int = 3) -> list:
    """Top-n emotions for a party EXCLUDING low-signal ones (configurable, e.g.
    neutral/approval), derived from the full scores so real signal isn't crowded out."""
    skip = low_signal if low_signal is not None else LOW_SIGNAL_EMOTIONS
    scores = party.get("scores") or {}
    if scores:
        ranked = sorted((e for e in scores if e not in skip),
                        key=lambda e: scores[e], reverse=True)
        return ranked[:n]
    return [e for e in (party.get("top3") or []) if e not in skip][:n]


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


async def _fetch_round_enrichment(round_ids: list, conv_ids: list) -> tuple[dict, dict, dict, dict]:
    """Sentiment + LLM mood scores keyed by ROUND id; topic & mode keyed by parent CONVERSATION id."""
    sentiment_map: dict = {}
    mood_scores_map: dict = {}
    topic_map: dict = {}
    mode_map: dict = {}
    conn = await get_ego_db()
    try:
        if round_ids:
            ph = ",".join("?" * len(round_ids))
            cursor = await conn.execute(
                f"SELECT key, value FROM module_data WHERE module='sentiment' AND key IN ({ph})",
                round_ids,
            )
            for row in await cursor.fetchall():
                try:
                    sentiment_map[row[0]] = json.loads(row[1])
                except Exception:
                    pass
            cursor = await conn.execute(
                f"SELECT key, value FROM module_data WHERE module='mood_scores' AND key IN ({ph})",
                round_ids,
            )
            for row in await cursor.fetchall():
                try:
                    mood_scores_map[row[0]] = json.loads(row[1])
                except Exception:
                    pass
        if conv_ids:
            ph = ",".join("?" * len(conv_ids))
            cursor = await conn.execute(
                f"SELECT key, value FROM module_data WHERE module='topic' AND key IN ({ph})", conv_ids
            )
            for row in await cursor.fetchall():
                topic_map[row[0]] = row[1]
            cursor = await conn.execute(
                f"SELECT key, value FROM module_data WHERE module='mode' AND key IN ({ph})", conv_ids
            )
            for row in await cursor.fetchall():
                mode_map[row[0]] = row[1]
    finally:
        await conn.close()
    return sentiment_map, mood_scores_map, topic_map, mode_map


async def _build_round_enriched(profile_name: str, db_path: str | None) -> list:
    """Recent rounds as mood data points: each round's own sentiment + its parent
    conversation's topic & mode (inherited). Newest first."""
    from .conversations import sync_recent_conversations, get_recent_rounds
    from .settings_store import get_low_signal_emotions
    await sync_recent_conversations(profile_name, db_path=db_path)
    rounds = await get_recent_rounds(profile_name, limit=await _lookback_rounds())
    if not rounds:
        return []
    round_ids = [r["id"] for r in rounds]
    conv_ids = list({r["conversation_id"] for r in rounds})
    sentiment_map, mood_scores_map, topic_map, mode_map = await _fetch_round_enrichment(round_ids, conv_ids)
    low_signal = await get_low_signal_emotions()

    enriched = []
    for r in rounds:
        cid = r["conversation_id"]
        sdata = sentiment_map.get(r["id"], {})
        u = sdata.get("user", {}) if sdata else {}
        a = sdata.get("agent", {}) if sdata else {}
        enriched.append({
            "id": r["id"], "conversation_id": cid,
            "round_index": r.get("round_index"),
            "start_ts": r.get("start_ts"), "end_ts": r.get("end_ts"),
            "msg_count": r.get("msg_count"),
            "mode": mode_map.get(cid), "topic": topic_map.get(cid),
            "mood_scores": mood_scores_map.get(r["id"]) or {},
            "sentiment_user": u.get("dominant"), "sentiment_agent": a.get("dominant"),
            "sentiment_user_top3": _top_emotions(u, low_signal),
            "sentiment_agent_top3": _top_emotions(a, low_signal),
            "user_scores": u.get("scores") or {}, "agent_scores": a.get("scores") or {},
            "user_msg_count": u.get("message_count"), "agent_msg_count": a.get("message_count"),
        })
    return enriched


def _rule_item_predicate(rule: dict):
    """Per-round predicate `(round) -> bool` for the per-item rule types — the exact
    per-round condition `_rule_fires` aggregates over a window. Returns None for rule
    types that aren't a per-round signal (currently only prev_mood, which depends on the
    cached mood, not on any single round). Shared so per-round match display and the real
    firing logic can't diverge."""
    p = rule["params"]
    rt = rule["rule_type"]

    if rt in ("mode_streak", "mode_count"):
        target = p.get("mode", "")
        negate = bool(p.get("negate", False))
        return lambda s: (s.get("mode") != target) if negate else (s.get("mode") == target)

    elif rt == "sentiment_user":
        # Match against the top-3 (not just the dominant emotion, which is almost
        # always 'neutral' and would keep these rules from ever firing).
        emotions = set(p.get("emotions", []))
        return lambda s: bool(emotions & set(s.get("sentiment_user_top3") or []))

    elif rt == "sentiment_agent":
        emotions = set(p.get("emotions", []))
        return lambda s: bool(emotions & set(s.get("sentiment_agent_top3") or []))

    elif rt == "sentiment_mismatch":
        emotions = set(p.get("emotions", []))
        direction = p.get("direction", "either")

        def _mismatches(s: dict) -> bool:
            u3 = set(s.get("sentiment_user_top3") or [])
            a3 = set(s.get("sentiment_agent_top3") or [])
            if direction == "user_only":
                return bool(emotions & (u3 - a3))
            elif direction == "agent_only":
                return bool(emotions & (a3 - u3))
            else:  # either
                return bool(emotions & (u3 - a3)) or bool(emotions & (a3 - u3))

        return _mismatches

    elif rt == "topic_keyword":
        keywords = [k.lower() for k in p.get("keywords", [])]
        if not keywords:
            return lambda s: False
        return lambda s: bool(s.get("topic") and any(kw in s["topic"].lower() for kw in keywords))

    return None


def _rule_fires(rule: dict, enriched: list, cached_mood_id: str | None = None) -> bool:
    p = rule["params"]
    rt = rule["rule_type"]

    if rt == "prev_mood":
        target = set(p.get("moods", []))
        if not target:
            return False
        in_set = cached_mood_id in target
        return (not in_set) if bool(p.get("negate", False)) else in_set

    pred = _rule_item_predicate(rule)
    if pred is None:
        return False

    if rt == "mode_streak":
        count = max(1, int(p.get("count", 3)))
        window = enriched[:count]
        if len(window) < count:
            return False
        return all(pred(s) for s in window)

    # mode_count, sentiment_user, sentiment_agent, sentiment_mismatch, topic_keyword:
    # count how many of the last `lookback` rounds satisfy the per-item predicate.
    default_lookback = 5 if rt in ("mode_count", "topic_keyword") else 1
    default_min = 2 if rt == "mode_count" else 1
    lookback = max(1, int(p.get("lookback", default_lookback)))
    min_count = max(1, int(p.get("min_count", default_min)))
    return sum(1 for s in enriched[:lookback] if pred(s)) >= min_count


def _round_matched_rules(rules: list, round_enriched: dict, moods: dict,
                         cached_mood_id: str | None = None) -> list:
    """Which active rules' per-round signal THIS single round satisfies, for the debug
    expansion. Excludes prev_mood (not a per-round signal). Each entry: {label, mood_name}."""
    matched = []
    for rule in rules:
        pred = _rule_item_predicate(rule)
        if pred is None:
            continue
        try:
            if pred(round_enriched):
                mid = rule["mood_id"]
                matched.append({
                    "label": rule.get("label") or _rule_label(rule),
                    "mood_name": moods[mid]["name"] if mid in moods else mid,
                })
        except Exception:
            pass
    return matched


async def _llm_vote_config() -> tuple[bool, float, int]:
    """(enabled, threshold, weight) for LLM mood votes, from settings."""
    from .settings_store import get_setting
    enabled = (await get_setting("llm_mood_votes_enabled", "1")) == "1"
    try:
        threshold = float(await get_setting("llm_mood_threshold", "6"))
    except (TypeError, ValueError):
        threshold = 6.0
    try:
        weight = max(1, int(float(await get_setting("llm_mood_weight", "1"))))
    except (TypeError, ValueError):
        weight = 1
    return enabled, threshold, weight


def _llm_mood_votes(enriched: list, moods: dict, threshold: float, weight: int) -> tuple[dict, list]:
    """Per-round threshold voting from the LLM's mood scores: each round where a mood scores
    >= threshold casts `weight` votes, summed across the (lookback-bounded) window.
    Returns ({mood_id: votes}, breakdown_lines)."""
    counts: dict[str, int] = {}
    for s in enriched:
        for mid, score in (s.get("mood_scores") or {}).items():
            if mid not in moods:
                continue
            try:
                if float(score) >= threshold:
                    counts[mid] = counts.get(mid, 0) + 1
            except (TypeError, ValueError):
                pass
    votes = {mid: n * weight for mid, n in counts.items()}
    breakdown = [f"LLM: {moods[mid]['name']} in {n} round(s) → +{n * weight}"
                 for mid, n in sorted(counts.items(), key=lambda x: -x[1])]
    return votes, breakdown


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

    enriched = await _build_round_enriched(profile_name, db_path)
    if not enriched:
        await _cache_result(profile_name, None, 0, [])
        return None

    vote_map: dict[str, int] = {}
    breakdown: list[str] = []
    for rule in rules:
        if rule["mood_id"] not in moods:
            continue
        # Mood gate: skip rule if current cached mood doesn't match
        if rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id:
            continue
        if _rule_fires(rule, enriched, cached_mood_id):
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + 1
            label = rule.get("label") or _rule_label(rule)
            breakdown.append(label)

    # LLM mood predictions vote alongside rules (can carry a mood on their own).
    enabled, thr, wt = await _llm_vote_config()
    if enabled:
        llm_votes, llm_breakdown = _llm_mood_votes(enriched, moods, thr, wt)
        for mid, v in llm_votes.items():
            vote_map[mid] = vote_map.get(mid, 0) + v
        breakdown += llm_breakdown

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

    enriched = await _build_round_enriched(profile_name, db_path)

    from .settings_store import get_low_signal_emotions
    low_signal = sorted(await get_low_signal_emotions())
    for r in enriched:
        r["matched_rules"] = _round_matched_rules(rules, r, moods, cached_mood_id)

    def _threshold(mid: str) -> int:
        return thresholds.get(mid, moods[mid]["min_votes"] if mid in moods else 1)

    vote_map: dict[str, int] = {}
    rule_results = []
    for rule in rules:
        in_catalog = rule["mood_id"] in moods
        gated = bool(rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id)
        fired = in_catalog and not gated and _rule_fires(rule, enriched, cached_mood_id)
        if fired:
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + 1
        rule_results.append({
            "label": rule.get("label") or _rule_label(rule),
            "mood_id": rule["mood_id"], "rule_type": rule["rule_type"],
            "gated": gated, "mood_gate": rule.get("mood_gate"), "fired": fired,
        })

    # LLM mood votes (tracked separately so the tally can show their contribution).
    llm_enabled, llm_thr, llm_wt = await _llm_vote_config()
    llm_votes, llm_breakdown = (_llm_mood_votes(enriched, moods, llm_thr, llm_wt)
                                if llm_enabled else ({}, []))
    for mid, v in llm_votes.items():
        vote_map[mid] = vote_map.get(mid, 0) + v

    tally = []
    for mid, votes in sorted(vote_map.items(), key=lambda x: -x[1]):
        th = _threshold(mid)
        lv = llm_votes.get(mid, 0)
        tally.append({
            "mood_id": mid, "name": moods[mid]["name"] if mid in moods else mid,
            "votes": votes, "threshold": th, "meets": votes >= th,
            "rule_votes": votes - lv, "llm_votes": lv,
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
        "enriched": enriched,
        "rules": rule_results,
        "tally": tally,
        "winner": winner,
        "is_default": is_default,
        "default_set": [moods[m]["name"] for m in await _load_defaults(profile_name, moods)],
        "cached_mood": cached_mood_id,
        "conversation_count": len(enriched),
        "low_signal": low_signal,
        "llm_votes_enabled": llm_enabled,
        "llm_breakdown": llm_breakdown,
        "llm_threshold": llm_thr,
    }


def _rule_label(rule: dict) -> str:
    p = rule["params"]
    rt = rule["rule_type"]
    gate = f"[while {rule['mood_gate']}] " if rule.get("mood_gate") else ""
    if rt == "prev_mood":
        op = "is not" if p.get("negate") else "is"
        return f"{gate}Previous mood {op} {', '.join(p.get('moods', [])[:3])}"
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
