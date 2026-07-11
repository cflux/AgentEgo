"""Sidequest (solo-round) scorer — Phase 3.

Turns a self-directed inward impulse into a mood signal. Three inputs, reconciled (§5.3 of
docs/impulse_agency_plan.md):

  1. self-report      — the feeling she states in her closing line (positivity-biased first party)
  2. independent score — an LLM read of the EXPERIENCE itself, ignoring her self-narration (the guard)
  3. subject/affinity  — her stored opinion of what she did (disliked subject → negative valence)

The reconciled result is stored as a `solo_round` record (module_data) and later folded into the mood
lookback as a synthetic, de-rated round (see mood_corrections._solo_weight). Nothing here touches the
conversations/rounds tables, so conversation analytics stay clean.
"""
import json
import logging
import time
from uuid import uuid4

from ..db.ego import get_ego_db
from . import affinity_engine
from .llm_client import chat, LLMError
from .settings_store import get_emotion_taxonomy

logger = logging.getLogger(__name__)

# Experience-led reconciliation: the independent read is trusted over her self-report (the whole reason
# for a second, content-based input is to catch a cheerful narration of a genuinely sad experience).
_W_EXP = 0.6
_W_SELF = 0.4
_AFFINITY_TILT = 0.3   # how much a stored like/dislike of the subject nudges the round's valence

_SYS = (
    "You are an affective scorer for an AI agent's PRIVATE, self-directed activity (a 'sidequest' she did "
    "alone, no user present). You are given her own account of what she did and how it left her. Score it "
    "coldly and honestly.\n"
    "Return STRICT JSON with this shape:\n"
    "{\n"
    '  "subject": "<2-4 word topic of what she did>",\n'
    '  "experience": {"emotions": {"<label>": 0.0-1.0, ...}, "moods": {"<mood_id>": 0-10, ...}, "valence": -1.0..1.0},\n'
    '  "self_report": {"emotions": {"<label>": 0.0-1.0, ...}, "valence": -1.0..1.0}\n'
    "}\n"
    "- experience = how the CONTENT/events would genuinely leave someone, IGNORING how she says she feels "
    "(catch a sad thing narrated cheerfully).\n"
    "- self_report = ONLY the feeling she explicitly states (usually her final line); empty if she states none.\n"
    "- Use ONLY emotion labels from the provided taxonomy and mood ids from the provided catalog. Omit ones "
    "that don't apply rather than scoring them 0.\n"
    "- valence: net pleasant(+)/unpleasant(−) tone."
)


async def _moods_catalog() -> list[dict]:
    conn = await get_ego_db()
    try:
        cur = await conn.execute("SELECT id, name, description FROM moods ORDER BY name")
        return [{"id": r[0], "name": r[1], "description": r[2]} for r in await cur.fetchall()]
    finally:
        await conn.close()


def _clean_scores(raw: dict, allowed: set, lo: float, hi: float) -> dict:
    """Keep only allowed keys with numeric values clamped to [lo, hi]."""
    out: dict = {}
    for k, v in (raw or {}).items():
        if k in allowed:
            try:
                out[k] = max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                continue
    return out


def _blend(a: dict, b: dict, wa: float, wb: float) -> dict:
    """Weighted union of two {key: score} maps."""
    out: dict = {}
    for k in set(a) | set(b):
        out[k] = round(wa * float(a.get(k, 0.0)) + wb * float(b.get(k, 0.0)), 4)
    return out


async def score_sidequest(profile: str, response: str) -> dict | None:
    """Run the three-input scorer over a sidequest response. Returns the reconciled solo-round payload
    (no persistence), or None if the LLM read is unusable."""
    taxonomy = await get_emotion_taxonomy()
    moods = await _moods_catalog()
    emo_set = set(taxonomy)
    mood_ids = {m["id"] for m in moods}

    user = (
        f"EMOTION TAXONOMY (use only these labels): {', '.join(taxonomy)}\n\n"
        f"MOOD CATALOG (use only these ids):\n"
        + "\n".join(f"  {m['id']} — {m['name']}: {m['description'] or ''}" for m in moods)
        + f"\n\n--- HER ACCOUNT OF THE SIDEQUEST ---\n{response.strip()}\n\nScore it."
    )
    from .reflection_engine import _parse_json
    data: dict = {}
    for attempt in range(4):  # deepseek intermittently returns an empty completion (json mode); retry
        try:
            raw = await chat(
                [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
                response_json=True, max_tokens=500,
            )
        except LLMError as exc:
            logger.warning("sidequest scorer LLM error: %s", exc)
            return None
        data = _parse_json(raw)
        if data:
            break
    if not data:
        logger.warning("sidequest scorer got no parseable response after retries")
        return None

    exp = data.get("experience") or {}
    self_r = data.get("self_report") or {}
    subject = str(data.get("subject") or "").strip()

    exp_emo = _clean_scores(exp.get("emotions"), emo_set, 0.0, 1.0)
    self_emo = _clean_scores(self_r.get("emotions"), emo_set, 0.0, 1.0)
    mood_scores = _clean_scores(exp.get("moods"), mood_ids, 0.0, 10.0)
    if not exp_emo and not mood_scores:
        return None  # nothing scorable

    # Reconcile: experience-led emotion blend; experience mood_scores drive the backbone.
    agent_scores = _blend(exp_emo, self_emo, _W_EXP, _W_SELF)

    def _val(d):
        try:
            return max(-1.0, min(1.0, float(d.get("valence", 0.0))))
        except (TypeError, ValueError):
            return 0.0
    valence = _W_EXP * _val(exp) + _W_SELF * _val(self_r)

    # Input #3: fold her stored opinion of the subject (disliked subject → more negative).
    if subject:
        aff = await affinity_engine.find_affinity(profile, subject)
        if aff and aff.get("valence") is not None:
            valence += _AFFINITY_TILT * float(aff["valence"])
    valence = round(max(-1.0, min(1.0, valence)), 4)

    # her closing self-report line, kept verbatim for the record/UI
    self_report_text = response.strip().splitlines()[-1].strip() if response.strip() else ""

    return {
        "subject": subject,
        "agent_scores": agent_scores,
        "mood_scores": mood_scores,
        "valence": valence,
        "self_report": self_report_text,
    }


async def record_sidequest(profile: str, session_id: str, response: str) -> dict | None:
    """Score an inward sidequest and persist it as a `solo_round`. Returns the stored record, or None if
    it couldn't be scored."""
    scored = await score_sidequest(profile, response)
    if not scored:
        return None
    record = {
        "profile": profile,
        "kind": "inward",
        "at": time.time(),
        "response": response,
        **scored,
    }
    key = session_id or str(uuid4())
    conn = await get_ego_db()
    try:
        await conn.execute(
            "INSERT INTO module_data (module, key, value, updated_at) VALUES ('solo_round', ?, ?, ?) "
            "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(record), record["at"]),
        )
        await conn.commit()
    finally:
        await conn.close()
    logger.info("solo_round scored (%s): valence=%.2f subject=%r", profile, record["valence"], record["subject"])
    return record


async def recent_solo_enriched(profile: str, limit: int = 40) -> list[dict]:
    """Recent solo rounds as mood-engine `enriched` dicts (newest first), tagged `solo` and carrying the
    de-rating valence. Same shape mood_engine._build_round_enriched emits, so the vote functions read them
    with no special-casing beyond mood_corrections._solo_weight."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT key, value, updated_at FROM module_data WHERE module = 'solo_round' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,),
        )
        rows = await cur.fetchall()
    finally:
        await conn.close()
    out: list[dict] = []
    for key, value, updated_at in rows:
        try:
            rec = json.loads(value)
        except (ValueError, TypeError):
            continue
        if rec.get("profile") != profile:
            continue
        out.append({
            "id": f"solo:{key}", "conversation_id": None,
            "solo": True, "valence": float(rec.get("valence", 0.0)),
            "end_ts": rec.get("at", updated_at), "start_ts": rec.get("at", updated_at),
            "mode": None, "topic": rec.get("subject"),
            "mood_scores": rec.get("mood_scores") or {},
            "agent_scores": rec.get("agent_scores") or {}, "user_scores": {},
        })
    return out
