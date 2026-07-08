import json
import time
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
        sdata = sentiment_map.get(r["id"]) or {}
        # A party can be explicitly null (e.g. a round with no agent messages), so coerce to {}.
        u = sdata.get("user") or {}
        a = sdata.get("agent") or {}
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


SENTIMENT_RULE_TYPES = ("sentiment_user", "sentiment_agent", "sentiment_mismatch", "sentiment_match")


def _sentiment_hits(rule: dict, enriched: list, window: int | None = None) -> list:
    """Per-anchor-round match booleans for a sentiment rule, newest-first. Anchors iterate the
    first `window` rounds (all rounds if None). For match/mismatch, `fuzz` allows the two parties'
    emotions to fall within F rounds of each other (fuzz=0 = same round) — neighbor lookups index
    the FULL list so an edge anchor can still see a neighbor just outside the window."""
    p = rule["params"]
    rt = rule["rule_type"]
    emotions = set(p.get("emotions", []))
    n = len(enriched)
    anchors = range(n if window is None else min(window, n))

    def uset(i):
        return set(enriched[i].get("sentiment_user_top3") or [])

    def aset(i):
        return set(enriched[i].get("sentiment_agent_top3") or [])

    if rt == "sentiment_user":
        return [bool(emotions & uset(i)) for i in anchors]
    if rt == "sentiment_agent":
        return [bool(emotions & aset(i)) for i in anchors]

    fuzz = max(0, int(p.get("fuzz", 0) or 0))

    def neighbors(i):
        return range(max(0, i - fuzz), min(n - 1, i + fuzz) + 1)

    if rt == "sentiment_match":
        hits = []
        for i in anchors:
            u_i, a_i = uset(i), aset(i)
            nb = list(neighbors(i))
            matched = False
            for e in emotions:
                if (e in u_i and any(e in aset(j) for j in nb)) or \
                   (e in a_i and any(e in uset(j) for j in nb)):
                    matched = True
                    break
            hits.append(matched)
        return hits

    if rt == "sentiment_mismatch":
        direction = p.get("direction", "either")
        hits = []
        for i in anchors:
            u_i, a_i = uset(i), aset(i)
            nb = list(neighbors(i))
            uo = any(all(e not in aset(j) for j in nb) for e in (emotions & u_i))
            ao = any(all(e not in uset(j) for j in nb) for e in (emotions & a_i))
            if direction == "user_only":
                hits.append(uo)
            elif direction == "agent_only":
                hits.append(ao)
            else:
                hits.append(uo or ao)
        return hits

    return [False for _ in anchors]


def _nonsentiment_fires(rule: dict, enriched: list, cached_mood_id: str | None = None) -> bool:
    """Boolean firing for the non-sentiment rule types (prev_mood / mode_streak / mode_count /
    topic_keyword). Each casts a single vote — no cumulative/streak scaling."""
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

    # mode_count, topic_keyword: count how many of the last `lookback` rounds satisfy the predicate.
    default_lookback = 5
    default_min = 2 if rt == "mode_count" else 1
    lookback = max(1, int(p.get("lookback", default_lookback)))
    min_count = max(1, int(p.get("min_count", default_min)))
    return sum(1 for s in enriched[:lookback] if pred(s)) >= min_count


def _rule_votes(rule: dict, enriched: list, cached_mood_id: str | None = None) -> int:
    """Vote weight a rule casts (0 = doesn't fire). Non-sentiment rules cast 1. Sentiment rules
    support cumulative counts (count mode) and streak scaling (streak mode); the vote is bounded
    by the rounds evaluated, so no explicit cap is needed."""
    p = rule["params"]
    rt = rule["rule_type"]

    if rt not in SENTIMENT_RULE_TYPES:
        return 1 if _nonsentiment_fires(rule, enriched, cached_mood_id) else 0

    if p.get("agg") == "streak":
        # Streak = consecutive matches from the newest round; scanned across all evaluated rounds.
        hits = _sentiment_hits(rule, enriched, window=None)
        streak = 0
        for h in hits:
            if h:
                streak += 1
            else:
                break
        min_streak = max(1, int(p.get("min_streak", 2)))
        if streak < min_streak:
            return 0
        return streak if p.get("scale") else 1

    # count (default): how many of the last `lookback` rounds match.
    lookback = max(1, int(p.get("lookback", 1)))
    n = sum(1 for h in _sentiment_hits(rule, enriched, window=lookback) if h)
    min_count = max(1, int(p.get("min_count", 1)))
    if n < min_count:
        return 0
    return n if p.get("cumulative") else 1


def _rule_fires(rule: dict, enriched: list, cached_mood_id: str | None = None) -> bool:
    return _rule_votes(rule, enriched, cached_mood_id) > 0


def _round_matched_rules(rules: list, enriched: list, idx: int, moods: dict,
                         cached_mood_id: str | None = None) -> list:
    """Which active rules' per-round signal the round at `idx` satisfies, for the debug
    expansion. Excludes prev_mood (not a per-round signal). Sentiment rules use the temporal
    hit list (so match/mismatch fuzz is reflected). Each entry: {label, mood_name}."""
    matched = []
    for rule in rules:
        try:
            if rule["rule_type"] in SENTIMENT_RULE_TYPES:
                hit = _sentiment_hits(rule, enriched, window=None)[idx]
            else:
                pred = _rule_item_predicate(rule)
                if pred is None:
                    continue
                hit = pred(enriched[idx])
            if hit:
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


def _transition_effective(vote_map: dict, moods: dict, cached_mood_id, tcfg: dict,
                          bias: float = 0.0, cascade: dict | None = None) -> tuple[dict, set | None, dict]:
    """Transition shaping. Applies the tenure-shaped incumbent `bias` (mutates vote_map): positive
    while the mood is fresh (anti-chatter), ~0 near grace (hold on genuine votes), negative-unbounded
    when overstayed (anti-stuck escape valve). The negative portion also erodes the incumbent's
    reverse-cascade chain so a feeder can't just re-escalate it. Then penalizes moods not adjacent to
    the current one — so the mood steps to a neighbor (or stays) unless a far mood clearly overpowers
    it. Returns (effective_votes, allowed_set_or_None, info)."""
    penalty = tcfg.get("penalty", 0)
    allowed = None
    applied = 0
    feeder_chain: set = set()
    if tcfg.get("enabled") and cached_mood_id in moods:
        if bias:
            vote_map[cached_mood_id] = vote_map.get(cached_mood_id, 0) + bias
            applied = bias
        # The incumbent's cascade feeders are tracked ONLY for the cooldown-destination guard (so we
        # never bar a mood we're handing off to). They are NOT decayed — feeders are legitimate
        # alternative moods, and dragging them down with the incumbent blocks a genuine handoff (e.g.
        # an affectionate stretch could never surface as Affectionate while Creative — which it feeds
        # — was the overstayed incumbent). Re-cascade bounce is handled by the post-eviction cooldown.
        if cascade:
            feeder_chain = _reverse_cascade_chain(cached_mood_id, cascade)
        allowed = set(tcfg.get("adjacency", {}).get(cached_mood_id, set())) | {cached_mood_id}
    effective = {}
    for mid, v in vote_map.items():
        pen = penalty if (allowed is not None and mid not in allowed) else 0
        effective[mid] = v - pen
    return effective, allowed, {"bias": applied, "penalty": penalty, "cached": cached_mood_id,
                                "enabled": bool(allowed is not None), "feeder_chain": feeder_chain}


def _reverse_cascade_chain(target, cascade: dict) -> set:
    """All moods whose cascade chain resolves INTO `target` (plus target itself). Flirty->Horny
    means Flirty *drives* Horny, so decaying Horny must also decay Flirty or it just re-cascades."""
    result = {target}
    changed = True
    while changed:
        changed = False
        for m, c in cascade.items():
            if c.get("to") in result and m not in result:
                result.add(m)
                changed = True
    return result


async def _rounds_since(profile_name: str, ts) -> int:
    if not ts:
        return 0
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE profile_name = ? AND end_ts > ?", (profile_name, ts)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()


async def _mood_change_at(profile_name: str):
    """Timestamp the current mood was set (latest mood_history change)."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT changed_at FROM mood_history WHERE profile_name = ? ORDER BY changed_at DESC LIMIT 1",
            (profile_name,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def _get_mood_cooldown(profile_name: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module = '_mood_cooldown' AND key = ?", (profile_name,)
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        await conn.close()


async def _set_mood_cooldown(profile_name: str, mood_id: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('_mood_cooldown', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (profile_name, json.dumps({"mood": mood_id, "at": time.time()}), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _get_mood_seed(profile_name: str) -> dict | None:
    """A 'morning seed' planted by a reset (reflection wake): {mood, at}. While no round is newer
    than `at`, evaluate_mood serves this mood instead of recomputing (the reset holds overnight)."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module = '_mood_seed' AND key = ?", (profile_name,)
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        await conn.close()


async def _set_mood_seed(profile_name: str, mood_id: str, note: list | None = None) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('_mood_seed', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (profile_name, json.dumps({"mood": mood_id, "at": time.time(), "note": note}), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _clear_mood_seed(profile_name: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM module_data WHERE module = '_mood_seed' AND key = ?", (profile_name,))
        await conn.commit()
    finally:
        await conn.close()


async def _newest_round_ts(profile_name: str) -> float | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT MAX(end_ts) FROM rounds WHERE profile_name = ?", (profile_name,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        await conn.close()


async def reset_mood(profile_name: str, mood_id: str, note: list | None = None) -> dict | None:
    """Reset the cached mood and plant a morning seed so it holds until new activity arrives.
    Used by the reflection 'wake' endpoint. `note` is the breakdown shown in the panel (e.g. why
    this mood was chosen). Returns the mood dict, or None if mood_id is unknown."""
    moods = await _load_moods()
    if mood_id not in moods:
        return None
    note = note or ["Reset (waking)"]
    await _set_mood_seed(profile_name, mood_id, note)
    await _cache_result(profile_name, mood_id, 0, note)
    return {**moods[mood_id], "vote_count": 0, "breakdown": note}


def _incumbent_bias(tenure: int, tcfg: dict, decay_cfg: dict) -> int:
    """Tenure-shaped bias carried by the incumbent (homeostasis v2). B = inertia − max(0, tenure −
    grace) * rate: +inertia while fresh (anti-chatter) → ~0 near grace (hold on genuine votes) →
    negative-unbounded when overstayed (the anti-stuck escape valve that eventually overtakes any
    self-reinforced lead, cascade or not). 0 when transitions are disabled; constant inertia when
    decay is disabled."""
    if not tcfg.get("enabled"):
        return 0
    inertia = tcfg.get("inertia", 0)
    if not decay_cfg.get("enabled"):
        return inertia
    return inertia - max(0, tenure - decay_cfg.get("grace", 0)) * decay_cfg.get("rate", 0)


async def _mood_tenure(profile_name: str) -> int:
    """Rounds the current mood has been held (since the last mood_history change)."""
    return await _rounds_since(profile_name, await _mood_change_at(profile_name))


async def _cooldown_excluded(profile_name: str, decay_cfg: dict, cascade: dict) -> set:
    """Bar ONLY the just-vacated mood from winning for grace + cooldown_buffer rounds — anchored to
    outlast the NEW mood's grace so the lookback signal turns over before it's eligible again (no synced
    A→B→A bounce). We deliberately do NOT bar its reverse-cascade feeders: they're legitimate alternative
    moods, and barring the whole chain muted a family of moods (e.g. Affectionate/Playful/Hopeful when
    Creative cooled) — including correction targets. A feeder that cascades into the barred mood simply
    can't win anyway (the target stays excluded), so the anti-bounce still holds. `cascade` kept for
    signature stability."""
    if not decay_cfg.get("enabled"):
        return set()
    cd = await _get_mood_cooldown(profile_name)
    if not cd or not cd.get("mood"):
        return set()
    bar = decay_cfg.get("grace", 0) + decay_cfg.get("cooldown", 0)
    if await _rounds_since(profile_name, cd.get("at", 0)) < bar:
        return {cd["mood"]}
    return set()


async def _moods_last_used(profile_name: str, mids: list) -> dict:
    """{mood_id: last changed_at} for the given moods, for least-recently-used tie-breaking."""
    if not mids:
        return {}
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(mids))
        cursor = await conn.execute(
            f"SELECT mood_id, MAX(changed_at) FROM mood_history WHERE profile_name = ? "
            f"AND mood_id IN ({ph}) GROUP BY mood_id",
            [profile_name, *mids],
        )
        return {r[0]: r[1] for r in await cursor.fetchall()}
    finally:
        await conn.close()


async def _select_winner(profile_name: str, candidates: list, effective: dict, cached_mood_id,
                         margin: int, thresh_of, moods: dict) -> str:
    """Choose the winning mood from eligible `candidates` [(mood_id, effective)].
    Promotion hysteresis: keep the incumbent unless a challenger beats its effective votes by
    `margin` (so noise near bias≈0 doesn't flip). On a genuine change, pick the destination — by
    default the strongest, or (if mood_fuzzy_select) the least-recently-used among challengers within
    mood_fuzzy_band of the top."""
    from .settings_store import get_setting
    cand_map = dict(candidates)
    if cached_mood_id in cand_map:
        challengers = [(m, e) for m, e in candidates if m != cached_mood_id]
        if not challengers:
            return cached_mood_id
        best_e = max(e for _, e in challengers)
        if best_e < cand_map[cached_mood_id] + margin:
            return cached_mood_id
    pool = [(m, e) for m, e in candidates if m != cached_mood_id] or list(candidates)
    if (await get_setting("mood_fuzzy_select", "0")) == "1":
        try:
            band = float(await get_setting("mood_fuzzy_band", "2"))
        except (TypeError, ValueError):
            band = 2.0
        top_e = max(e for _, e in pool)
        near = [m for m, e in pool if e >= top_e - band]
        if len(near) > 1:
            last = await _moods_last_used(profile_name, near)
            return min(near, key=lambda m: (last.get(m) or 0.0, thresh_of(m)))
    return max(pool, key=lambda x: (x[1], thresh_of(x[0])))[0]


def _cascade_leads_to(src: str, cascade: dict, target) -> bool:
    """True if `target` lies downstream of `src` along the cascade chain (excluding `src` itself)."""
    seen: set = set()
    cur = src
    while cur in cascade and cur not in seen:
        seen.add(cur)
        cur = cascade[cur].get("to")
        if cur == target:
            return True
    return False


def _cascade_transfer(vote_map: dict, moods: dict, cached_mood_id, cascade: dict,
                      enabled: bool) -> tuple[dict, list, dict]:
    """Fold intensity cascades into vote formation: when a source mood's EARNED votes clear its
    escalation threshold, transfer them onto the target BEFORE winner selection — so the target
    competes (and then holds, via inertia) on genuine support, instead of being hijacked for a
    single round and bouncing back. Hysteresis: once escalated (the current mood is the target or
    downstream of the source) the source only needs to stay >= `release`; otherwise it must reach
    `at`. Runs to a fixpoint so chains (A→B→C) flow through, zeroing each consumed source so votes
    are never double-counted. Returns (new_vote_map, notes, net_delta_per_mood)."""
    result = dict(vote_map)
    notes: list = []
    deltas: dict = {}
    if not enabled or not cascade:
        return result, notes, deltas
    for _ in range(len(cascade) + 1):
        changed = False
        for src, edge in cascade.items():
            tgt = edge.get("to")
            if tgt not in moods:
                continue
            v = result.get(src, 0)
            if v <= 0:
                continue
            escalated = (cached_mood_id == tgt) or _cascade_leads_to(src, cascade, cached_mood_id)
            thr = edge.get("release", edge.get("at", 99)) if escalated else edge.get("at", 99)
            if v >= thr:
                result[tgt] = result.get(tgt, 0) + v
                result[src] = 0
                deltas[tgt] = deltas.get(tgt, 0) + v
                deltas[src] = deltas.get(src, 0) - v
                src_name = moods[src]["name"] if src in moods else src
                notes.append(f"{src_name} ({v}≥{thr}) → {moods[tgt]['name']}")
                changed = True
        if not changed:
            break
    return result, notes, deltas


async def _cache_result(profile_name: str, mood_id, votes: int, breakdown: list) -> None:
    conn = await get_ego_db()
    try:
        # Log a history row only when the mood actually changes.
        cursor = await conn.execute(
            "SELECT mood_id FROM agent_moods WHERE profile_name = ?", (profile_name,)
        )
        row = await cursor.fetchone()
        prev_mood_id = row[0] if row else None
        changed = mood_id != prev_mood_id and not (mood_id is None and prev_mood_id is None)
        if changed:
            await conn.execute(
                "INSERT INTO mood_history (profile_name, prev_mood_id, mood_id, vote_count, breakdown, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (profile_name, prev_mood_id, mood_id, votes, json.dumps(breakdown), time.time()),
            )
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
    # Optional: write the disposition block to a file on mood change (blank setting = HTTP-only).
    if changed:
        await _write_directive_file(profile_name, mood_id)


async def _write_directive_file(profile_name: str, mood_id) -> None:
    """Best-effort: on a mood change, write the guardrailed disposition block to the configured
    file so a file-based Hermes prompt can include it. No-op unless mood_directive_file is set."""
    from .settings_store import get_setting
    path = (await get_setting("mood_directive_file", "") or "").strip()
    if not path or (await get_setting("mood_directive_enabled", "1")) != "1":
        return
    try:
        if not mood_id:
            body = ""
        else:
            conn = await get_ego_db()
            try:
                cursor = await conn.execute("SELECT name, description FROM moods WHERE id = ?", (mood_id,))
                row = await cursor.fetchone()
            finally:
                await conn.close()
            name, desc = (row[0], row[1]) if row else (mood_id, "")
            template = await get_setting("mood_directive_template", "")
            body = (template or "").replace("{mood}", name or "").replace("{description}", desc or "").strip()
        import os
        with open(os.path.expanduser(path), "w") as f:
            f.write(body + ("\n" if body else ""))
    except Exception:
        pass


async def refresh_all_moods() -> None:
    """Recompute + cache every profile's mood on a schedule, so the agent-facing endpoints
    (`/api/mood/directive`, `/api/mood/current`) are pure reads of the cached value — compute is
    decoupled from the fetch. Round-based decay/tenure means the cadence only needs to keep pace
    with new rounds, not with how often the agent looks."""
    from .profiles import discover_profiles
    for p in discover_profiles():
        try:
            await evaluate_mood(p["name"], db_path=p["db_path"])
        except Exception:
            pass


async def get_cached_mood(profile_name: str) -> dict | None:
    """The last computed mood for a profile (pure read of agent_moods; no recompute)."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT am.mood_id, am.vote_count, am.breakdown, m.name, m.description, m.color, m.icon "
            "FROM agent_moods am LEFT JOIN moods m ON m.id = am.mood_id WHERE am.profile_name = ?",
            (profile_name,),
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if not row or not row[0]:
        return None
    try:
        breakdown = json.loads(row[2]) if row[2] else []
    except Exception:
        breakdown = []
    return {"id": row[0], "vote_count": row[1] or 0, "breakdown": breakdown,
            "name": row[3] or row[0], "description": row[4] or "", "color": row[5], "icon": row[6]}


async def _generate_legacy_votes(profile_name: str, enriched: list, moods: dict,
                                 cached_mood_id, rules: list) -> tuple[dict, list]:
    """Legacy vote generation: the 57-rule engine + discrete LLM mood votes."""
    vote_map: dict = {}
    breakdown: list[str] = []
    for rule in rules:
        if rule["mood_id"] not in moods:
            continue
        if rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id:
            continue
        v = _rule_votes(rule, enriched, cached_mood_id)
        if v:
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + v
            label = rule.get("label") or _rule_label(rule)
            breakdown.append(f"{label} (+{v})" if v > 1 else label)
    enabled, thr, wt = await _llm_vote_config()
    if enabled:
        llm_votes, llm_breakdown = _llm_mood_votes(enriched, moods, thr, wt)
        for mid, v in llm_votes.items():
            vote_map[mid] = vote_map.get(mid, 0) + v
        breakdown += llm_breakdown
    return vote_map, breakdown


async def _generate_v2_votes(profile_name: str, enriched: list, moods: dict) -> tuple[dict, list, dict]:
    """v2 vote generation: continuous LLM backbone + per-profile corrections (mood_corrections)."""
    from . import mood_corrections as MC
    vote_map, dbg = await MC.v2_vote_map(profile_name, enriched, moods)
    breakdown: list[str] = []
    bb = dbg.get("backbone", {})
    top = sorted((m for m in bb if m in moods), key=lambda m: -bb[m])[:4]
    if top:
        breakdown.append("Backbone (LLM): " + ", ".join(f"{moods[m]['name']} {bb[m]:.1f}" for m in top))
    for m, v in dbg.get("corrections", {}).items():
        if m in moods and v:
            breakdown.append(f"Correction: {moods[m]['name']} +{v:.1f}")
    return vote_map, breakdown, dbg


async def _resolve_mood(profile_name: str, vote_map: dict, breakdown: list, moods: dict,
                        cached_mood_id, thresholds: dict, *, commit: bool = True) -> dict | None:
    """The shared shaping + selection layer (cascade → tenure-bias → cooldown → hysteresis → winner),
    identical for legacy and v2. `commit=False` = pure read (no cooldown set, no cache) for shadow."""
    from .settings_store import get_transition_config, get_mood_cascade, get_mood_decay_config, get_setting
    tcfg = await get_transition_config()
    casc_enabled, cascade = await get_mood_cascade()
    decay_cfg = await get_mood_decay_config()
    tenure = await _mood_tenure(profile_name)

    resting_scores = dict(vote_map)  # pre-cascade backbone read — resting settles on the calm signal
    vote_map, casc_notes, _ = _cascade_transfer(vote_map, moods, cached_mood_id, cascade, casc_enabled)
    if casc_notes:
        breakdown.append("Cascade: " + " → ".join(casc_notes))

    bias = _incumbent_bias(tenure, tcfg, decay_cfg)
    effective, _allowed, tinfo = _transition_effective(vote_map, moods, cached_mood_id, tcfg, bias, cascade)
    if tinfo["bias"] and cached_mood_id in moods:
        nm = moods[cached_mood_id]["name"]; b = tinfo["bias"]
        breakdown.append(f"Hold +{b} ({nm}, {tenure} rounds)" if b > 0
                         else f"Anti-stuck {b} (held {nm} {tenure} rounds)")

    cooldown_excluded = await _cooldown_excluded(profile_name, decay_cfg, cascade)

    def _threshold(mid: str) -> int:
        return thresholds.get(mid, moods[mid]["min_votes"])

    candidates = [(mid, effective[mid]) for mid in vote_map
                  if effective[mid] >= _threshold(mid) and mid not in cooldown_excluded]

    if not candidates:
        defaults = await _load_defaults(profile_name, moods)
        if not defaults:
            if commit:
                await _cache_result(profile_name, None, 0, [])
            return None
        # Backbone-weighted resting: settle into the resting mood the backbone faintly favours rather than
        # a blind random pick (continuous with the v2 backbone). Stay put on a tie so we don't drift on noise.
        ranked = sorted(defaults, key=lambda m: resting_scores.get(m, 0.0), reverse=True)
        top = ranked[0]
        top_score = resting_scores.get(top, 0.0)
        # Keep the current resting mood on a (near-)tie to avoid drifting on noise.
        if cached_mood_id in defaults and resting_scores.get(cached_mood_id, 0.0) >= top_score - 1e-9:
            chosen = cached_mood_id
        else:
            chosen = top
        chosen_score = resting_scores.get(chosen, 0.0)
        note = f"Resting ({moods[chosen]['name']})" + (f" · backbone {chosen_score:.1f}" if chosen_score else "")
        if commit:
            await _cache_result(profile_name, chosen, 0, [note])
        return {**moods[chosen], "vote_count": 0, "breakdown": [note], "is_default": True}

    try:
        margin = int(float(await get_setting("mood_switch_margin", "1")))
    except (TypeError, ValueError):
        margin = 1
    winner_id = await _select_winner(profile_name, candidates, effective, cached_mood_id, margin, _threshold, moods)
    winner_votes = vote_map[winner_id]

    if commit and cached_mood_id and winner_id != cached_mood_id and tinfo["bias"] < 0 \
            and winner_id not in tinfo["feeder_chain"]:
        await _set_mood_cooldown(profile_name, cached_mood_id)

    winner = {**moods[winner_id], "vote_count": round(winner_votes, 2), "breakdown": breakdown}
    if commit:
        await _cache_result(profile_name, winner_id, int(round(winner_votes)), breakdown)
    return winner


async def _log_shadow(profile_name: str, legacy: dict | None, v2: dict | None, v2_debug: dict) -> None:
    """Record a legacy-vs-v2 comparison for the shadow soak: running agreement + recent disagreements."""
    lw = (legacy or {}).get("id"); vw = (v2 or {}).get("id")
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT value FROM module_data WHERE module='mood_shadow' AND key=?", (profile_name,))
        row = await cur.fetchone()
        rec = json.loads(row[0]) if row else {"total": 0, "agree": 0, "disagreements": []}
        rec["total"] += 1
        if lw == vw:
            rec["agree"] += 1
        else:
            rec["disagreements"] = ([{
                "at": time.time(),
                "legacy": (legacy or {}).get("name"), "legacy_votes": (legacy or {}).get("vote_count"),
                "v2": (v2 or {}).get("name"), "v2_votes": (v2 or {}).get("vote_count"),
                "backbone": v2_debug.get("backbone"), "corrections": v2_debug.get("corrections"),
            }] + rec.get("disagreements", []))[:25]
        rec["last_at"] = time.time()
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('mood_shadow', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (profile_name, json.dumps(rec), time.time()))
        await conn.commit()
    finally:
        await conn.close()


async def _apply_exits(profile_name: str, winner: dict | None, cached_mood_id, moods: dict,
                       enriched: list) -> dict | None:
    """Post-resolution exit-trigger override (mood_exits). If an exit fires, snap to its target, cooldown
    the mood being left, and re-cache. Runs on the *driving* winner only; skips default/seed moods."""
    if not winner or winner.get("is_default") or winner.get("is_seed") or winner.get("id") not in moods:
        return winner
    try:
        from . import mood_exits
        ex = await mood_exits.evaluate_exits(profile_name, winner["id"], cached_mood_id, moods, enriched)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("mood exit eval failed: %s", exc)
        return winner
    if not ex:
        return winner
    await _set_mood_cooldown(profile_name, ex["from_mood"])
    breakdown = [ex["note"]]
    await _cache_result(profile_name, ex["target"], 0, breakdown)
    return {**moods[ex["target"]], "vote_count": 0, "breakdown": breakdown, "exited": True}


async def evaluate_mood(profile_name: str, db_path: str | None = None) -> dict | None:
    """
    Evaluate mood rules for a profile using threshold voting.
    Returns {id, name, color, icon, vote_count, breakdown} or None.
    Caches the result in agent_moods.
    """
    moods = await _load_moods()

    # Morning-seed hold (reflection wake): a reset sticks until a round newer than it appears.
    # Checked first — before rules/activity gates — so a reset always holds. Sync only on this rare
    # path (so the normal path isn't double-synced) to detect and yield to genuinely new activity.
    seed = await _get_mood_seed(profile_name)
    if seed and seed.get("mood") in moods:
        from .conversations import sync_recent_conversations
        await sync_recent_conversations(profile_name, db_path=db_path)
        newest = await _newest_round_ts(profile_name)
        if newest is None or newest <= seed.get("at", 0):
            note = seed.get("note") or ["Woke into this mood"]
            await _cache_result(profile_name, seed["mood"], 0, note)
            return {**moods[seed["mood"]], "vote_count": 0, "breakdown": note, "is_seed": True}
        await _clear_mood_seed(profile_name)  # new activity supersedes the reset

    if not moods:
        await _cache_result(profile_name, None, 0, [])
        return None

    thresholds = await _load_thresholds(profile_name)
    cached_mood_id = await _load_cached_mood(profile_name)

    enriched = await _build_round_enriched(profile_name, db_path)
    if not enriched:
        await _cache_result(profile_name, None, 0, [])
        return None

    from .settings_store import get_setting
    mode = await get_setting("mood_scoring_mode", "legacy")

    # Mood scoring v2 drives directly (LLM backbone + corrections → same shaping layer). We still run
    # the legacy engine READ-ONLY (reverse-shadow) so the /corrective comparison lens keeps working —
    # it just no longer affects the agent.
    if mode == "corrective":
        v2vm, v2bd, v2dbg = await _generate_v2_votes(profile_name, enriched, moods)
        legacy_w = None
        try:
            rules = await _load_rules(profile_name)
            if rules:
                lvm, lbd = await _generate_legacy_votes(profile_name, enriched, moods, cached_mood_id, rules)
                legacy_w = await _resolve_mood(profile_name, lvm, lbd, moods, cached_mood_id,
                                               thresholds, commit=False)
        except Exception:
            pass
        winner = await _resolve_mood(profile_name, v2vm, v2bd, moods, cached_mood_id, thresholds, commit=True)
        winner = await _apply_exits(profile_name, winner, cached_mood_id, moods, enriched)
        if legacy_w is not None:
            try:
                await _log_shadow(profile_name, legacy_w, winner, v2dbg)
            except Exception:
                pass
        return winner

    # legacy drives (also under shadow — which additionally computes + logs v2 for comparison).
    rules = await _load_rules(profile_name)
    if not rules:
        await _cache_result(profile_name, None, 0, [])
        return None
    vote_map, breakdown = await _generate_legacy_votes(profile_name, enriched, moods, cached_mood_id, rules)
    winner = await _resolve_mood(profile_name, vote_map, breakdown, moods, cached_mood_id,
                                 thresholds, commit=True)
    winner = await _apply_exits(profile_name, winner, cached_mood_id, moods, enriched)
    if mode == "shadow":
        try:
            v2vm, v2bd, v2dbg = await _generate_v2_votes(profile_name, enriched, moods)
            v2_winner = await _resolve_mood(profile_name, v2vm, v2bd, moods, cached_mood_id,
                                            thresholds, commit=False)
            await _log_shadow(profile_name, winner, v2_winner, v2dbg)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("mood shadow log failed: %s", exc)
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
    for idx, r in enumerate(enriched):
        r["matched_rules"] = _round_matched_rules(rules, enriched, idx, moods, cached_mood_id)

    def _threshold(mid: str) -> int:
        return thresholds.get(mid, moods[mid]["min_votes"] if mid in moods else 1)

    vote_map: dict[str, int] = {}
    rule_results = []
    for rule in rules:
        in_catalog = rule["mood_id"] in moods
        gated = bool(rule.get("mood_gate") and rule["mood_gate"] != cached_mood_id)
        v = _rule_votes(rule, enriched, cached_mood_id) if (in_catalog and not gated) else 0
        fired = v > 0
        if fired:
            vote_map[rule["mood_id"]] = vote_map.get(rule["mood_id"], 0) + v
        rule_results.append({
            "label": rule.get("label") or _rule_label(rule),
            "mood_id": rule["mood_id"], "rule_type": rule["rule_type"],
            "gated": gated, "mood_gate": rule.get("mood_gate"), "fired": fired,
            "votes": v,
        })

    # LLM mood votes (tracked separately so the tally can show their contribution).
    llm_enabled, llm_thr, llm_wt = await _llm_vote_config()
    llm_votes, llm_breakdown = (_llm_mood_votes(enriched, moods, llm_thr, llm_wt)
                                if llm_enabled else ({}, []))
    for mid, v in llm_votes.items():
        vote_map[mid] = vote_map.get(mid, 0) + v

    from .settings_store import get_transition_config, get_mood_cascade, get_mood_decay_config, get_setting
    tcfg = await get_transition_config()
    _casc_enabled, cascade = await get_mood_cascade()
    decay_cfg = await get_mood_decay_config()
    tenure = await _mood_tenure(profile_name)

    # Intensity cascades fold into vote formation (earned votes, before the incumbent bias).
    vote_map, cascade_notes, casc_deltas = _cascade_transfer(vote_map, moods, cached_mood_id, cascade, _casc_enabled)

    # Homeostasis v2: tenure-shaped incumbent bias + non-adjacent penalty (read-only mirror of
    # evaluate_mood; does not set the cooldown).
    bias = _incumbent_bias(tenure, tcfg, decay_cfg)
    effective, allowed, tinfo = _transition_effective(vote_map, moods, cached_mood_id, tcfg, bias, cascade)
    cooldown_excluded = await _cooldown_excluded(profile_name, decay_cfg, cascade)

    tally = []
    for mid, votes in sorted(vote_map.items(), key=lambda x: -effective[x[0]]):
        th = _threshold(mid)
        lv = llm_votes.get(mid, 0)
        casc_here = casc_deltas.get(mid, 0)
        bias_here = tinfo["bias"] if (mid == cached_mood_id) else 0
        penalized = allowed is not None and mid not in allowed
        decayed_here = mid == cached_mood_id and tinfo["bias"] < 0
        on_cooldown = mid in cooldown_excluded
        tally.append({
            "mood_id": mid, "name": moods[mid]["name"] if mid in moods else mid,
            "votes": votes, "effective": effective[mid], "threshold": th,
            "meets": effective[mid] >= th and not on_cooldown,
            "rule_votes": votes - lv - bias_here - casc_here, "llm_votes": lv,
            "cascade": casc_here, "inertia": bias_here if bias_here > 0 else 0,
            "anti_stuck": -bias_here if bias_here < 0 else 0, "penalized": penalized,
            "decayed": decayed_here, "on_cooldown": on_cooldown,
        })

    candidates = [(mid, effective[mid]) for mid in vote_map
                  if effective[mid] >= _threshold(mid) and mid not in cooldown_excluded]
    winner = None
    is_default = False
    if candidates:
        try:
            margin = int(float(await get_setting("mood_switch_margin", "1")))
        except (TypeError, ValueError):
            margin = 1
        wid = await _select_winner(profile_name, candidates, effective, cached_mood_id, margin, _threshold, moods)
        winner = {"id": wid, "name": moods[wid]["name"], "votes": vote_map.get(wid, 0)}
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
        "transitions_enabled": tinfo["enabled"],
        "inertia_bonus": tinfo["bias"] if tinfo["bias"] > 0 else 0,
        "bias": tinfo["bias"],
        "jump_penalty": tinfo["penalty"],
        "allowed_moves": sorted(moods[m]["name"] for m in allowed if m in moods) if allowed else [],
        "cascade": cascade_notes,
        "decay_enabled": decay_cfg["enabled"],
        "tenure": tenure,
        "decay": -tinfo["bias"] if tinfo["bias"] < 0 else 0,
        "decayed_chain": ([moods[cached_mood_id]["name"]]
                          if (tinfo["bias"] < 0 and cached_mood_id in moods) else []),
        "cooldown_moods": sorted(moods[m]["name"] for m in cooldown_excluded if m in moods),
    }


def _sentiment_suffix(p: dict) -> str:
    """Human hint for the aggregation/fuzz knobs on a sentiment rule."""
    bits = []
    if p.get("agg") == "streak":
        s = f"streak ≥{max(1, int(p.get('min_streak', 2)))}"
        if p.get("scale"):
            s += ", ×streak"
        bits.append(s)
    elif p.get("cumulative"):
        bits.append("cumulative")
    fz = int(p.get("fuzz", 0) or 0)
    if fz:
        bits.append(f"±{fz} round" + ("s" if fz != 1 else ""))
    return f" ({'; '.join(bits)})" if bits else ""


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
        return f"{gate}User felt {emo} recently{_sentiment_suffix(p)}"
    elif rt == "sentiment_agent":
        emo = ", ".join(p.get("emotions", [])[:3])
        return f"{gate}Agent expressed {emo} recently{_sentiment_suffix(p)}"
    elif rt == "sentiment_match":
        emo = ", ".join(p.get("emotions", [])[:3])
        return f"{gate}Both felt {emo}{_sentiment_suffix(p)}"
    elif rt == "sentiment_mismatch":
        emo = ", ".join(p.get("emotions", [])[:3])
        dir_map = {"user_only": "user/not agent", "agent_only": "agent/not user"}
        direction = dir_map.get(p.get("direction", "either"), "either direction")
        return f"{gate}Mismatch ({emo}) — {direction}{_sentiment_suffix(p)}"
    elif rt == "topic_keyword":
        kw = ", ".join(p.get("keywords", [])[:3])
        return f"{gate}Topic contained '{kw}'"
    return rt
