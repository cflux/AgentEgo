"""Topic canonicalization — collapse near-duplicate topic labels while still
admitting genuinely new concepts.

Topics are open-ended (the worker invents 1-3 word labels), which produces variants
like 'tech support' / 'technical support' / 'support chat'. On save we ask the
configured LLM whether a new topic means the same as an existing one; if so we map
it to the existing canonical, otherwise we keep it as a new topic. Decisions are
cached in the topic_aliases table so the LLM is only consulted once per raw string.
"""
import time
from ..db.ego import get_ego_db
from .llm_client import chat, LLMError

_MATCH_SYSTEM = (
    "You deduplicate short conversation topic labels (1-3 words). Given a list of EXISTING topics "
    "and a NEW topic, decide whether the NEW one means essentially the same thing as one of the "
    "existing topics.\n"
    "Reply with ONLY the existing topic it matches (copied EXACTLY), or the single token NEW if it "
    "is a genuinely distinct concept.\n"
    "Be conservative: merge clear synonyms / rephrasings (e.g. 'tech support' = 'technical support' "
    "= 'support chat'; 'image generation' = 'image gen'), but keep different concepts separate "
    "(e.g. 'video editing' vs 'video generation', 'morning routine' vs 'nightlife')."
)


async def _llm_match(raw: str, existing: list[str]) -> str | None:
    """Return the existing topic the raw one matches, or None for a new concept."""
    if not existing:
        return None
    listing = ", ".join(existing[:120])
    try:
        out = await chat(
            [{"role": "system", "content": _MATCH_SYSTEM},
             {"role": "user", "content": f"EXISTING: {listing}\nNEW: {raw}"}],
            max_tokens=400,
        )
    except LLMError:
        return None  # no LLM → fall back to keeping the raw topic
    ans = (out or "").strip().strip('"').strip().lower()
    if not ans or ans == "new":
        return None
    for e in existing:
        if e.lower() == ans:
            return e
    return None


async def _existing_topics() -> list[str]:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT DISTINCT value FROM module_data WHERE module='topic' AND value IS NOT NULL AND value <> ''"
        )
        return sorted({r[0] for r in await cursor.fetchall()})
    finally:
        await conn.close()


async def canonicalize_topic(raw: str) -> str:
    """Map a freshly generated topic to a canonical one (cached); new concepts pass through."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    key = raw.lower()

    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT canonical FROM topic_aliases WHERE raw = ?", (key,))
        row = await cursor.fetchone()
        if row:
            return row[0]
    finally:
        await conn.close()

    existing = await _existing_topics()
    canonical = next((e for e in existing if e.lower() == key), None)
    if canonical is None:
        canonical = await _llm_match(raw, existing)
    if canonical is None:
        canonical = raw  # genuinely new concept

    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO topic_aliases (raw, canonical, created_at) VALUES (?, ?, ?)",
            (key, canonical, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return canonical


async def backfill_topics() -> dict:
    """One-time: collapse the existing topic vocabulary. Builds canonicals from the
    most common labels down, rewrites module_data, and records the alias map."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            "SELECT value, COUNT(*) c FROM module_data WHERE module='topic' AND value IS NOT NULL AND value <> '' "
            "GROUP BY value ORDER BY c DESC"
        )
        raw_topics = [r[0] for r in await cursor.fetchall()]
    finally:
        await conn.close()

    canonicals: list[str] = []
    mapping: dict[str, str] = {}
    for raw in raw_topics:
        match = next((c for c in canonicals if c.lower() == raw.lower()), None)
        if match is None:
            match = await _llm_match(raw, canonicals)
        if match is None:
            canonicals.append(raw)
            match = raw
        mapping[raw] = match

    now = time.time()
    conn = await get_ego_db()
    try:
        for raw, canon in mapping.items():
            await conn.execute(
                "INSERT OR REPLACE INTO topic_aliases (raw, canonical, created_at) VALUES (?, ?, ?)",
                (raw.lower(), canon, now),
            )
            if canon != raw:
                await conn.execute(
                    "UPDATE module_data SET value = ? WHERE module='topic' AND value = ?", (canon, raw)
                )
        await conn.commit()
    finally:
        await conn.close()
    return {"raw": len(raw_topics), "canonical": len(canonicals),
            "merged": sum(1 for r, c in mapping.items() if r != c)}
