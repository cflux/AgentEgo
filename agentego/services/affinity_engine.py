"""Affinity (likes / dislikes / interests) engine with bounded evolution.

Preferences are anchored to a SOUL.md-derived baseline but evolve as the agent
encounters topics over time. Seeded (explicit) affinities are clamped to a band
around their baseline so the character can shift in nuance but never flip; emergent
affinities move within global bounds and gain confidence with repeated mentions.
"""
import json
import re
import time
import random
from uuid import uuid4
from ..db.ego import get_ego_db
from .settings_store import get_evolution_config, get_setting

OCEAN_KEYS = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]

# Conversational filler words. A topic made up ENTIRELY of these (e.g. "greeting chat",
# "general conversation", "name") carries no preference signal and is excluded from the
# affinity ledger. A topic with any substantive word (e.g. "python debugging") is kept.
_FILLER_WORDS = {
    "greeting", "greetings", "hello", "hi", "hey", "yo", "name", "names", "intro",
    "introduction", "chat", "chats", "chatting", "chitchat", "smalltalk", "talk",
    "talking", "casual", "general", "generic", "conversation", "convo", "discussion",
    "check", "checkin", "checking", "in", "catchup", "goodbye", "bye", "farewell",
    "thanks", "thank", "you", "test", "testing", "untitled", "misc", "miscellaneous",
    "random", "stuff", "things", "question", "questions", "query", "help", "request",
    "status", "update", "the", "a", "an", "and", "of", "to", "about", "some",
    "small", "quick", "brief", "casual", "friendly",
}


def is_meaningful_topic(topic: str) -> bool:
    """False for empty/too-short topics or pure conversational filler."""
    t = (topic or "").strip().lower()
    if len(t) < 3:
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", t) if w]
    if not words:
        return False
    return not all(w in _FILLER_WORDS for w in words)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# --- Traits substrate ---

async def get_traits(profile_name: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT source_hash, traits_baseline, traits_current, extracted_at, updated_at "
            "FROM personality_traits WHERE profile_name = ?",
            (profile_name,),
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if not row:
        return None
    try:
        baseline = json.loads(row[1]) if row[1] else {}
    except Exception:
        baseline = {}
    try:
        current = json.loads(row[2]) if row[2] else baseline
    except Exception:
        current = baseline
    return {
        "source_hash": row[0],
        "baseline": baseline,
        "current": current,
        "extracted_at": row[3],
        "updated_at": row[4],
    }


async def save_traits(profile_name: str, source_hash: str, traits: dict) -> None:
    """Store freshly extracted traits as both baseline and current (resets drift)."""
    now = time.time()
    payload = json.dumps(traits)
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO personality_traits
                (profile_name, source_hash, traits_baseline, traits_current, extracted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
                source_hash = excluded.source_hash,
                traits_baseline = excluded.traits_baseline,
                traits_current = excluded.traits_current,
                extracted_at = excluded.extracted_at,
                updated_at = excluded.updated_at
            """,
            (profile_name, source_hash, payload, payload, now, now),
        )
        await conn.commit()
    finally:
        await conn.close()


async def evolve_traits(profile_name: str, proposed_ocean: dict) -> dict | None:
    """Nudge traits_current toward a proposed OCEAN vector, clamped within
    trait_drift_delta of the baseline so the personality can't escape itself."""
    traits = await get_traits(profile_name)
    if not traits:
        return None
    cfg = await get_evolution_config()
    delta = cfg["trait_drift"]
    base_ocean = (traits["baseline"] or {}).get("ocean", {})
    new_current = dict(traits["current"] or traits["baseline"])
    new_ocean = dict(new_current.get("ocean", {}))
    for k in OCEAN_KEYS:
        if k in proposed_ocean and k in base_ocean:
            lo = base_ocean[k] - delta
            hi = base_ocean[k] + delta
            new_ocean[k] = _clamp(_clamp(proposed_ocean[k], 0.0, 1.0), lo, hi)
    new_current["ocean"] = new_ocean

    now = time.time()
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE personality_traits SET traits_current = ?, updated_at = ? WHERE profile_name = ?",
            (json.dumps(new_current), now, profile_name),
        )
        await conn.commit()
    finally:
        await conn.close()
    return new_current


# --- Affinity ledger ---

async def get_affinities(profile_name: str) -> list[dict]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT id, entity, category, valence, intensity, confidence,
                   baseline_valence, baseline_intensity, source, rationale,
                   mention_count, first_seen, last_seen, updated_at
            FROM affinities WHERE profile_name = ?
            ORDER BY (valence * intensity) DESC
            """,
            (profile_name,),
        )
        rows = await cursor.fetchall()
    finally:
        await conn.close()
    out = []
    for r in rows:
        drift = None
        if r[6] is not None:
            drift = round(r[3] - r[6], 3)
        out.append({
            "id": r[0], "entity": r[1], "category": r[2],
            "valence": r[3], "intensity": r[4], "confidence": r[5],
            "baseline_valence": r[6], "baseline_intensity": r[7],
            "source": r[8], "rationale": r[9], "mention_count": r[10],
            "first_seen": r[11], "last_seen": r[12], "updated_at": r[13],
            "valence_drift": drift,
            "score": round(r[3] * r[4], 3),
        })
    return out


async def get_affinity(profile_name: str, entity: str) -> dict | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT valence, intensity, confidence, baseline_valence, baseline_intensity, "
            "source, mention_count FROM affinities WHERE profile_name = ? AND entity = ?",
            (profile_name, entity),
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if not row:
        return None
    return {
        "valence": row[0], "intensity": row[1], "confidence": row[2],
        "baseline_valence": row[3], "baseline_intensity": row[4],
        "source": row[5], "mention_count": row[6],
    }


async def find_affinity(profile_name: str, subject: str) -> dict | None:
    """Case-insensitive lookup of a stored affinity for an arbitrary subject."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT entity, category, valence, intensity, confidence, source, rationale "
            "FROM affinities WHERE profile_name = ? AND LOWER(entity) = LOWER(?) LIMIT 1",
            (profile_name, subject.strip()),
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if not row:
        return None
    return {
        "entity": row[0], "category": row[1], "valence": row[2], "intensity": row[3],
        "confidence": row[4], "source": row[5], "rationale": row[6],
    }


def _weighted_sample(items: list, k: int, weight_fn) -> list:
    """Sample up to k items without replacement, weighted by weight_fn (salience).

    Stronger items appear more often, but the long tail still gets representation —
    so injected taste lists stay short AND vary between calls instead of always
    showing the same top entries."""
    pool = list(items)
    weights = [max(1e-6, weight_fn(it)) for it in pool]
    chosen = []
    for _ in range(min(k, len(pool))):
        total = sum(weights)
        r = random.uniform(0, total)
        cum = 0.0
        for i, w in enumerate(weights):
            cum += w
            if r <= cum:
                chosen.append(pool.pop(i))
                weights.pop(i)
                break
    return chosen


async def get_taste_context(profile_name: str, top_n: int = 6, sample: bool = False) -> dict:
    """Compact taste summary for prompt injection + the agent-facing profile API.

    sample=True draws a weighted random subset from a larger pool (for impulse
    prompts → variety); sample=False returns the deterministic top_n (for the
    agent-facing profile API → full, stable picture)."""
    if sample:
        try:
            pool_size = int(await get_setting("taste_pool_size", "15"))
            sample_size = int(await get_setting("taste_sample_size", "5"))
        except (TypeError, ValueError):
            pool_size, sample_size = 15, 5
        summary = await get_affinity_summary(profile_name, top_n=pool_size)
        likes = _weighted_sample(summary["likes"], sample_size, lambda a: a["score"])
        dislikes = _weighted_sample(summary["dislikes"], sample_size, lambda a: abs(a["score"]))
        interests = _weighted_sample(summary["interests"], sample_size, lambda a: a["intensity"])
    else:
        summary = await get_affinity_summary(profile_name, top_n=top_n)
        likes, dislikes, interests = summary["likes"], summary["dislikes"], summary["interests"]

    traits = await get_traits(profile_name)

    def _names(items: list) -> str:
        return ", ".join(a["entity"] for a in items) or "—"

    personality = ""
    values: list = []
    if traits:
        cur = traits["current"] or {}
        personality = cur.get("summary", "")
        values = cur.get("values", [])
    return {
        "likes": _names(likes),
        "dislikes": _names(dislikes),
        "interests": _names(interests),
        "personality": personality,
        "values": values,
        "summary": summary,
        "traits": traits,
    }


async def apply_observation(
    profile_name: str,
    entity: str,
    *,
    valence: float,
    intensity: float,
    confidence: float = 0.5,
    category: str | None = None,
    rationale: str | None = None,
    source: str = "inferred",
) -> dict:
    """Insert or evolve an affinity from a new observation.

    New entity → stored directly (and, if source='seed', the values also become the
    immutable baseline anchor). Existing entity → EMA blend into current, then
    regularize: seeded entities are clamped to a band around their baseline (no
    flips); emergent ones stay within global [-1,1]/[0,1] bounds.
    """
    entity = entity.strip()
    valence = _clamp(valence, -1.0, 1.0)
    intensity = _clamp(intensity, 0.0, 1.0)
    confidence = _clamp(confidence, 0.0, 1.0)
    now = time.time()

    existing = await get_affinity(profile_name, entity)
    # New INFERRED entity? Fold it into an existing near-identical one so we never store a dup.
    # (Seeds define the canonical set, so they're stored as-is.)
    if not existing and source != "seed":
        canonical = await _canonicalize_new_entity(profile_name, entity)
        if canonical != entity:
            entity = canonical
            existing = await get_affinity(profile_name, entity)
    cfg = await get_evolution_config()
    alpha = cfg["alpha"]
    band = cfg["seed_band"]

    conn = await get_ego_db()
    try:
        if not existing:
            base_v = valence if source == "seed" else None
            base_i = intensity if source == "seed" else None
            await conn.execute(
                """
                INSERT INTO affinities
                    (id, profile_name, entity, category, valence, intensity, confidence,
                     baseline_valence, baseline_intensity, source, rationale,
                     mention_count, first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (str(uuid4()), profile_name, entity, category, valence, intensity, confidence,
                 base_v, base_i, source, rationale, now, now, now),
            )
            await conn.commit()
            return {"action": "created", "entity": entity, "valence": valence, "intensity": intensity}

        # EMA blend toward the new observation
        new_v = alpha * valence + (1 - alpha) * existing["valence"]
        new_i = alpha * intensity + (1 - alpha) * existing["intensity"]

        if existing["baseline_valence"] is not None:
            # Seeded: clamp within band around baseline so it can't flip
            bv = existing["baseline_valence"]
            bi = existing["baseline_intensity"] if existing["baseline_intensity"] is not None else new_i
            new_v = _clamp(new_v, bv - band, bv + band)
            new_i = _clamp(new_i, bi - band, bi + band)
        new_v = _clamp(new_v, -1.0, 1.0)
        new_i = _clamp(new_i, 0.0, 1.0)

        # Confidence grows asymptotically with repeated observation
        new_conf = _clamp(existing["confidence"] + (1 - existing["confidence"]) * 0.25, 0.0, 1.0)

        await conn.execute(
            """
            UPDATE affinities SET
                valence = ?, intensity = ?, confidence = ?,
                category = COALESCE(?, category),
                rationale = COALESCE(?, rationale),
                mention_count = mention_count + 1,
                last_seen = ?, updated_at = ?
            WHERE profile_name = ? AND entity = ?
            """,
            (new_v, new_i, new_conf, category, rationale, now, now, profile_name, entity),
        )
        await conn.commit()
        return {"action": "evolved", "entity": entity, "valence": new_v, "intensity": new_i}
    finally:
        await conn.close()


# --- Experience signal (how the agent actually felt about a topic) ---

async def get_topic_sentiment(profile_name: str, topic: str) -> dict:
    """How the AGENT actually felt during conversations about `topic`: aggregate her emotion scores
    and the round mood scores across those conversations' rounds. Lets measured experience correct
    the trait-only guess. Returns {top_emotions, top_moods, rounds}; empty if nothing scored."""
    from .settings_store import get_low_signal_emotions
    empty = {"top_emotions": [], "top_moods": [], "rounds": 0}
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT c.id FROM conversations c JOIN module_data m "
            "ON m.module='topic' AND m.key=c.id "
            "WHERE c.profile_name=? AND m.value=?",
            (profile_name, topic),
        )
        conv_ids = [r[0] for r in await cursor.fetchall()]
        if not conv_ids:
            return empty
        cph = ",".join("?" * len(conv_ids))
        cursor = await conn.execute(
            f"SELECT id FROM rounds WHERE conversation_id IN ({cph})", conv_ids
        )
        round_ids = [r[0] for r in await cursor.fetchall()]
        if not round_ids:
            return empty
        rph = ",".join("?" * len(round_ids))
        cursor = await conn.execute(
            f"SELECT value FROM module_data WHERE module='sentiment' AND key IN ({rph})", round_ids
        )
        sent_rows = [r[0] for r in await cursor.fetchall()]
        cursor = await conn.execute(
            f"SELECT value FROM module_data WHERE module='mood_scores' AND key IN ({rph})", round_ids
        )
        mood_rows = [r[0] for r in await cursor.fetchall()]
        cursor = await conn.execute("SELECT id, name FROM moods")
        mood_names = {r[0]: r[1] for r in await cursor.fetchall()}
    finally:
        await conn.close()

    low = await get_low_signal_emotions()
    emo_totals: dict = {}
    for raw in sent_rows:
        try:
            agent = (json.loads(raw) or {}).get("agent") or {}
        except Exception:
            continue
        for k, v in (agent.get("scores") or {}).items():
            if k not in low:
                emo_totals[k] = emo_totals.get(k, 0.0) + float(v)
    top_emotions = [k for k, _ in sorted(emo_totals.items(), key=lambda x: -x[1])[:5]]

    mood_totals: dict = {}
    for raw in mood_rows:
        try:
            for k, v in (json.loads(raw) or {}).items():
                mood_totals[k] = mood_totals.get(k, 0.0) + float(v)
        except Exception:
            continue
    top_moods = [mood_names.get(k, k) for k, _ in sorted(mood_totals.items(), key=lambda x: -x[1])[:5]]
    return {"top_emotions": top_emotions, "top_moods": top_moods, "rounds": len(round_ids)}


async def get_refresh_entities(profile_name: str, limit: int = 200) -> list[str]:
    """Known topics whose conversations have rounds newer than their affinity's last update —
    candidates to re-observe from fresh experience (so existing affinities keep learning)."""
    from .conversations import get_recent_conversations
    conversations = await get_recent_conversations(profile_name, limit=limit)
    if not conversations:
        return []
    conv_ids = [c["id"] for c in conversations]
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(conv_ids))
        cursor = await conn.execute(
            f"SELECT key, value FROM module_data WHERE module='topic' AND key IN ({ph})", conv_ids
        )
        conv_topic = {r[0]: r[1].strip() for r in await cursor.fetchall() if r[1] and r[1].strip()}
        cursor = await conn.execute(
            "SELECT LOWER(entity), updated_at FROM affinities WHERE profile_name=?", (profile_name,)
        )
        aff_updated = {r[0]: (r[1] or 0) for r in await cursor.fetchall()}
        cursor = await conn.execute(
            f"SELECT conversation_id, MAX(end_ts) FROM rounds WHERE conversation_id IN ({ph}) "
            "GROUP BY conversation_id", conv_ids
        )
        newest_round = {r[0]: r[1] for r in await cursor.fetchall()}
    finally:
        await conn.close()
    refresh = set()
    for cid, topic in conv_topic.items():
        tl = topic.lower()
        if tl in aff_updated and is_meaningful_topic(topic):
            nr = newest_round.get(cid)
            if nr and nr > aff_updated[tl]:
                refresh.add(topic)
    return sorted(refresh)


# --- Pending inference queue ---

async def get_pending_entities(profile_name: str, limit: int = 200) -> list[str]:
    """Distinct conversation topics for this profile that have no affinity yet."""
    from .conversations import get_recent_conversations
    conversations = await get_recent_conversations(profile_name, limit=limit)
    if not conversations:
        return []
    conv_ids = [c["id"] for c in conversations]

    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(conv_ids))
        cursor = await conn.execute(
            f"SELECT DISTINCT value FROM module_data WHERE module='topic' AND key IN ({ph})",
            conv_ids,
        )
        topics = {row[0].strip() for row in await cursor.fetchall() if row[0] and row[0].strip()}

        cursor = await conn.execute(
            "SELECT entity FROM affinities WHERE profile_name = ?", (profile_name,)
        )
        known = {row[0].strip().lower() for row in await cursor.fetchall()}
    finally:
        await conn.close()
    return sorted(
        t for t in topics
        if t.lower() not in known and is_meaningful_topic(t)
    )


async def prune_generic_affinities(profile_name: str | None = None) -> int:
    """Delete already-stored emergent affinities that are pure conversational filler.

    Never removes SOUL-seeded affinities. Returns the number deleted."""
    conn = await get_ego_db()
    try:
        if profile_name:
            cursor = await conn.execute(
                "SELECT id, entity FROM affinities WHERE source != 'seed' AND profile_name = ?",
                (profile_name,),
            )
        else:
            cursor = await conn.execute(
                "SELECT id, entity FROM affinities WHERE source != 'seed'"
            )
        doomed = [row[0] for row in await cursor.fetchall() if not is_meaningful_topic(row[1])]
        for aid in doomed:
            await conn.execute("DELETE FROM affinities WHERE id = ?", (aid,))
        await conn.commit()
        return len(doomed)
    finally:
        await conn.close()


_DEDUPE_SYSTEM = (
    "You clean up an agent's list of liked/disliked things by finding DUPLICATES — entries that "
    "refer to the SAME underlying thing, just phrased differently (e.g. 'manga and anime' = "
    "'manga/anime'; 'athletic female bodies' = 'athletic women with large breasts'). Do NOT group "
    "things that are merely related but distinct, or different levels of granularity (e.g. 'art' vs "
    "'modern art' are different — keep separate unless clearly identical). For each duplicate group "
    "of 2+ entries, pick the clearest EXISTING member as the canonical. Return ONLY JSON: "
    '{"groups": [{"canonical": "<one of the members, verbatim>", "members": ["<verbatim>", ...]}]}. '
    "Omit anything with no duplicate."
)


async def _find_duplicate_groups(entities: list[str]) -> list[dict]:
    """Ask the LLM to group near-identical entities. Returns [{canonical, members}] (2+ members)."""
    from .llm_client import chat, LLMError
    listing = "\n".join(f"- {e}" for e in entities)
    try:
        raw = await chat(
            [{"role": "system", "content": _DEDUPE_SYSTEM},
             {"role": "user", "content": "Entries:\n" + listing}],
            response_json=True, max_tokens=1200,
        )
        data = json.loads(raw)
    except (LLMError, json.JSONDecodeError, ValueError):
        return []
    groups = data.get("groups") if isinstance(data, dict) else data
    known = {e.lower(): e for e in entities}
    out = []
    for g in (groups or []):
        members = [known[m.lower()] for m in g.get("members", []) if isinstance(m, str) and m.lower() in known]
        members = list(dict.fromkeys(members))  # dedupe, preserve order
        canon = g.get("canonical", "")
        canon = known.get(canon.lower()) if isinstance(canon, str) else None
        if canon and canon in members and len(members) >= 2:
            out.append({"canonical": canon, "members": members})
    return out


async def _merge_affinity_group(profile_name: str, canonical: str, members: list[str]) -> None:
    """Merge duplicate affinity rows into the canonical entity (mention-weighted valence/intensity,
    summed mentions, widest date span, seed status preserved), then delete the others."""
    conn = await get_ego_db()
    try:
        ph = ",".join("?" * len(members))
        cursor = await conn.execute(
            f"SELECT entity, valence, intensity, confidence, baseline_valence, baseline_intensity, "
            f"source, rationale, category, mention_count, first_seen, last_seen "
            f"FROM affinities WHERE profile_name = ? AND entity IN ({ph})",
            [profile_name, *members],
        )
        rows = await cursor.fetchall()
        if len(rows) < 2:
            return
        w = [max(int(r[9] or 1), 1) for r in rows]
        W = sum(w) or 1
        valence = round(sum(r[1] * wi for r, wi in zip(rows, w)) / W, 4)
        intensity = round(sum(r[2] * wi for r, wi in zip(rows, w)) / W, 4)
        confidence = max(r[3] for r in rows)
        mention_count = sum(int(r[9] or 0) for r in rows)
        first_seen = min((r[10] for r in rows if r[10] is not None), default=time.time())
        last_seen = max((r[11] for r in rows if r[11] is not None), default=time.time())
        source = "seed" if any(r[6] == "seed" for r in rows) else "inferred"
        canon = next((r for r in rows if r[0] == canonical), rows[0])
        # prefer a seed member's baseline (anchors SOUL-seeded evolution), else the canonical's
        seed_base = next((r for r in rows if r[6] == "seed" and r[4] is not None), None)
        base_v = seed_base[4] if seed_base else canon[4]
        base_i = seed_base[5] if seed_base else canon[5]
        await conn.execute(
            """
            UPDATE affinities SET valence=?, intensity=?, confidence=?, baseline_valence=?,
                   baseline_intensity=?, source=?, mention_count=?, first_seen=?, last_seen=?,
                   updated_at=? WHERE profile_name=? AND entity=?
            """,
            (valence, intensity, confidence, base_v, base_i, source, mention_count,
             first_seen, last_seen, time.time(), profile_name, canonical),
        )
        losers = [m for m in members if m != canonical]
        if losers:
            lp = ",".join("?" * len(losers))
            await conn.execute(
                f"DELETE FROM affinities WHERE profile_name = ? AND entity IN ({lp})",
                [profile_name, *losers],
            )
        await conn.commit()
    finally:
        await conn.close()


_CANON_SYSTEM = (
    "You deduplicate an agent's affinity list. Given a NEW entry and the EXISTING entries, decide "
    "if the new entry refers to the SAME underlying thing as one of the existing entries (a "
    "duplicate or rephrasing). If yes, reply with that EXISTING entry VERBATIM. If it's genuinely "
    "distinct — or merely related / a different granularity — reply exactly: NEW. Reply with ONLY "
    "the existing entry text or NEW, nothing else."
)


async def _alias_cache_get(profile_name: str, raw: str) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM module_data WHERE module='affinity_alias' AND key=?",
            (f"{profile_name}|{raw.lower()}",),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def _alias_cache_set(profile_name: str, raw: str, canonical: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('affinity_alias', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (f"{profile_name}|{raw.lower()}", canonical, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _canonicalize_new_entity(profile_name: str, entity: str) -> str:
    """Map a brand-new inferred entity onto an existing near-identical one (so we don't create a
    duplicate row). LLM match-or-NEW, cached per (profile, raw). Fails open to `entity`."""
    if (await get_setting("affinity_dedupe_enabled", "1")) != "1":
        return entity
    existing = [a["entity"] for a in await get_affinities(profile_name)]
    if not existing:
        return entity
    existing_lower = {e.lower(): e for e in existing}
    cached = await _alias_cache_get(profile_name, entity)
    if cached is not None:
        # empty string = "distinct"; otherwise the canonical (if it still exists)
        return existing_lower.get(cached.lower(), entity) if cached else entity
    from .llm_client import chat, LLMError
    try:
        listing = "\n".join(f"- {e}" for e in existing)
        raw = await chat(
            [{"role": "system", "content": _CANON_SYSTEM},
             {"role": "user", "content": f"NEW: {entity}\nEXISTING:\n{listing}"}],
            max_tokens=1000,  # DeepSeek reasoning models spend budget before the answer token
        )
        ans = (raw or "").strip().strip('".').strip()
    except LLMError:
        return entity
    canonical = None if ans.upper() == "NEW" else existing_lower.get(ans.lower())
    await _alias_cache_set(profile_name, entity, canonical or "")
    return canonical or entity


async def dedupe_affinities(profile_name: str) -> dict:
    """LLM-group near-identical affinities and merge each group into one canonical entry."""
    affinities = await get_affinities(profile_name)
    entities = [a["entity"] for a in affinities]
    if len(entities) < 2:
        return {"merged": 0, "removed": 0, "groups": []}
    groups = await _find_duplicate_groups(entities)
    removed = 0
    done = []
    for g in groups:
        await _merge_affinity_group(profile_name, g["canonical"], g["members"])
        removed += len(g["members"]) - 1
        done.append({"canonical": g["canonical"], "merged": g["members"]})
    return {"merged": len(done), "removed": removed, "groups": done}


# --- Dashboard helpers ---

async def get_affinity_summary(profile_name: str, top_n: int = 8) -> dict:
    """Likes / dislikes / interests views over the ledger."""
    affinities = await get_affinities(profile_name)
    likes = [a for a in affinities if a["valence"] >= 0.15]
    likes.sort(key=lambda a: a["score"], reverse=True)
    dislikes = [a for a in affinities if a["valence"] <= -0.15]
    dislikes.sort(key=lambda a: a["valence"])
    # Interests = things engaged with intensely that AREN'T dislikes — a strong dislike (rude
    # people, being ignored) is felt intensely but is a dislike, not an interest.
    interests = [a for a in affinities if a["intensity"] >= 0.5 and a["valence"] > -0.15]
    interests.sort(key=lambda a: a["intensity"], reverse=True)
    return {
        "all": affinities,
        "likes": likes[:top_n],
        "dislikes": dislikes[:top_n],
        "interests": interests[:top_n],
        "total": len(affinities),
        "emergent": len([a for a in affinities if a["source"] != "seed"]),
    }
