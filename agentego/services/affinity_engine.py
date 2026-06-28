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
            return {"action": "created", "valence": valence, "intensity": intensity}

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
        return {"action": "evolved", "valence": new_v, "intensity": new_i}
    finally:
        await conn.close()


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


# --- Dashboard helpers ---

async def get_affinity_summary(profile_name: str, top_n: int = 8) -> dict:
    """Likes / dislikes / interests views over the ledger."""
    affinities = await get_affinities(profile_name)
    likes = [a for a in affinities if a["valence"] >= 0.15]
    likes.sort(key=lambda a: a["score"], reverse=True)
    dislikes = [a for a in affinities if a["valence"] <= -0.15]
    dislikes.sort(key=lambda a: a["valence"])
    interests = [a for a in affinities if a["intensity"] >= 0.5]
    interests.sort(key=lambda a: a["intensity"], reverse=True)
    return {
        "all": affinities,
        "likes": likes[:top_n],
        "dislikes": dislikes[:top_n],
        "interests": interests[:top_n],
        "total": len(affinities),
        "emergent": len([a for a in affinities if a["source"] != "seed"]),
    }
