"""Mood corrections — the per-profile corrective layer (mood scoring v2).

The LLM per-round mood scores are the backbone; a *small* per-profile set of corrections nudges a mood's
support where the LLM systematically misses. A correction is one legible unit: an emotion→weight affinity
(agent party), an optional `mutual` accelerative bonus (user also feels it), an optional `mode`
undercurrent (steady baseline while a conversation mode is active), recency-weighted over the window.

This module owns: CRUD, derivation from the flip analysis (seeding), and the contribution math consumed
by the scoring path. Kept LLM-free and deterministic.
"""
import json
import time
from uuid import uuid4
from collections import Counter
from ..db.ego import get_ego_db

_COLS = ("id, profile_name, target_mood, agent_emotions, relation, user_emotions, mode, "
         "topic_contains, strength, note, enabled, created_at")


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _row(r) -> dict:
    return {
        "id": r[0], "profile_name": r[1], "target_mood": r[2],
        "agent_emotions": _j(r[3], {}), "relation": r[4] or "none",
        "user_emotions": _j(r[5], {}), "mode": _j(r[6], []),
        "topic_contains": _j(r[7], []), "strength": r[8], "note": r[9],
        "enabled": bool(r[10]), "created_at": r[11],
    }


# --- CRUD ---

async def list_corrections(profile_name: str, enabled_only: bool = False) -> list[dict]:
    conn = await get_ego_db()
    try:
        q = f"SELECT {_COLS} FROM mood_corrections WHERE profile_name = ?"
        if enabled_only:
            q += " AND enabled = 1"
        q += " ORDER BY created_at ASC"
        cur = await conn.execute(q, (profile_name,))
        return [_row(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def create_correction(profile_name: str, target_mood: str, agent_emotions: dict, *,
                            relation: str = "none", user_emotions: dict | None = None,
                            mode: list | None = None, topic_contains: list | None = None,
                            strength: float = 0.6, note: str = "", enabled: bool = True) -> str:
    cid = str(uuid4())
    conn = await get_ego_db()
    try:
        await conn.execute(
            f"INSERT INTO mood_corrections ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, profile_name, target_mood, json.dumps(agent_emotions or {}),
             relation if relation in ("none", "mutual", "mismatch") else "none",
             json.dumps(user_emotions or {}), json.dumps(mode or []), json.dumps(topic_contains or []),
             max(0.0, min(1.0, float(strength))), note, 1 if enabled else 0, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return cid


async def update_correction(cid: str, **fields) -> None:
    allowed = {"target_mood", "agent_emotions", "relation", "user_emotions", "mode",
               "topic_contains", "strength", "note", "enabled"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("agent_emotions", "user_emotions", "mode", "topic_contains"):
            v = json.dumps(v)
        elif k == "enabled":
            v = 1 if v else 0
        elif k == "strength":
            v = max(0.0, min(1.0, float(v)))
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    conn = await get_ego_db()
    try:
        await conn.execute(f"UPDATE mood_corrections SET {', '.join(sets)} WHERE id = ?", (*vals, cid))
        await conn.commit()
    finally:
        await conn.close()


async def delete_correction(cid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute("DELETE FROM mood_corrections WHERE id = ?", (cid,))
        await conn.commit()
    finally:
        await conn.close()


async def toggle_correction(cid: str) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            "UPDATE mood_corrections SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?", (cid,))
        await conn.commit()
    finally:
        await conn.close()


# --- Contribution (consumed by the scoring path) ---

async def get_correction_config() -> dict:
    """Global tuning knobs for the corrective layer (calibrated during shadow)."""
    from .settings_store import get_setting
    async def _f(k, d):
        try:
            return float(await get_setting(k, str(d)))
        except (TypeError, ValueError):
            return d
    return {
        "halflife": max(0.5, await _f("mood_recency_halflife", 8.0)),
        "backbone_scale": await _f("mood_backbone_scale", 2.5),
        "scale": await _f("mood_correction_scale", 0.5),
        "mutual_bonus": await _f("mood_correction_mutual_bonus", 0.5),
        "mode_baseline": await _f("mood_correction_mode_baseline", 0.1),
    }


def backbone_votes(enriched: list[dict], moods: dict, cfg: dict) -> dict:
    """Continuous backbone: recency-weighted sum of the local scorer's per-round mood scores (0–10,
    normalized to 0–1), unscaled. The caller applies backbone_scale."""
    hl = cfg["halflife"]
    out: dict = {}
    for i, rnd in enumerate(enriched):
        rec = 0.5 ** (i / hl)
        for mid, sc in (rnd.get("mood_scores") or {}).items():
            if mid in moods:
                out[mid] = out.get(mid, 0.0) + rec * (float(sc) / 10.0)
    return out


async def v2_vote_map(profile_name: str, enriched: list[dict], moods: dict,
                      cfg: dict | None = None) -> tuple[dict, dict]:
    """The mood-scoring-v2 vote map = backbone_scale × (continuous backbone + corrective contributions).
    Returns (vote_map, debug) where debug carries the unscaled parts + per-correction contributions for
    the UI. Fed into the SAME shaping layer (cascade/bias/cooldown/hysteresis) as legacy."""
    if cfg is None:
        cfg = await get_correction_config()
    bb = backbone_votes(enriched, moods, cfg)
    corrs = await list_corrections(profile_name, enabled_only=True)
    cv, per_corr = correction_votes(corrs, enriched, cfg)
    bs = cfg["backbone_scale"]
    vote_map: dict = {}
    for mid in set(bb) | set(cv):
        vote_map[mid] = round(bs * (bb.get(mid, 0.0) + cv.get(mid, 0.0)), 3)
    debug = {
        "backbone": {k: round(bs * v, 2) for k, v in bb.items()},
        "corrections": {k: round(bs * v, 2) for k, v in cv.items()},
        "per_correction": {k: round(bs * v, 2) for k, v in per_corr.items()},
    }
    return vote_map, debug


def correction_votes(corrections: list[dict], enriched: list[dict], cfg: dict) -> tuple[dict, dict]:
    """Per-mood corrective vote contributions over the window (enriched newest-first).
    Returns ({mood_id: votes}, {correction_id: contribution}) — the latter for the live UI readout."""
    halflife = cfg["halflife"]; scale = cfg["scale"]
    mutual_bonus = cfg["mutual_bonus"]; mode_baseline = cfg["mode_baseline"]
    per_mood: dict = {}
    per_corr: dict = {}
    for c in corrections:
        if not c.get("enabled"):
            continue
        emo = c.get("agent_emotions") or {}
        if not emo and not c.get("mode"):
            continue
        rel = c.get("relation", "none")
        umap = (c.get("user_emotions") or emo) if rel != "none" else {}
        modes = set(c.get("mode") or [])
        total = 0.0
        for i, rnd in enumerate(enriched):
            rec = 0.5 ** (i / halflife)  # newest round (i=0) weight 1.0, halving every `halflife` rounds
            ag = rnd.get("agent_scores") or {}
            term = sum(w * float(ag.get(e, 0.0)) for e, w in emo.items())
            if rel == "mutual":
                us = rnd.get("user_scores") or {}
                term += mutual_bonus * sum(w * float(us.get(e, 0.0)) for e, w in umap.items())
            if modes and rnd.get("mode") in modes:
                term += mode_baseline
            total += rec * term
        contrib = round(max(0.0, c.get("strength", 0.6)) * scale * total, 3)
        if contrib:
            per_mood[c["target_mood"]] = per_mood.get(c["target_mood"], 0.0) + contrib
            per_corr[c["id"]] = contrib
    return per_mood, per_corr


# --- Derivation from the flip analysis (seeding) ---

async def derive_corrections(profile_name: str, db_path: str | None = None,
                             lookback: int = 180, min_flips: int = 2) -> list[dict]:
    """Replay the current rules + LLM votes over historical raw enrichment; for each mood the rules
    genuinely FLIP the LLM toward (>= min_flips), consolidate that mood's rules' emotion sets into a
    proposed correction. Returns proposed corrections (unsaved)."""
    from . import mood_engine as ME
    from .profiles import resolve_profile
    from .conversations import get_recent_rounds, sync_recent_conversations
    from .settings_store import get_low_signal_emotions
    db_path = db_path or resolve_profile(profile_name)
    moods = await ME._load_moods()
    rules = [r for r in await ME._load_rules(profile_name) if r["rule_type"] != "prev_mood"]
    _, thr, wt = await ME._llm_vote_config()
    try:
        await sync_recent_conversations(profile_name, db_path=db_path)
    except Exception:
        pass
    rounds = await get_recent_rounds(profile_name, limit=lookback)
    rids = [r["id"] for r in rounds]; cids = list({r["conversation_id"] for r in rounds})
    sm, msm, tm, mm = await ME._fetch_round_enrichment(rids, cids)
    low = await get_low_signal_emotions()
    enr = []
    for r in rounds:
        cid = r["conversation_id"]; sd = sm.get(r["id"]) or {}; u = sd.get("user") or {}; a = sd.get("agent") or {}
        enr.append({"id": r["id"], "conversation_id": cid, "mode": mm.get(cid), "topic": tm.get(cid),
                    "mood_scores": msm.get(r["id"]) or {},
                    "sentiment_user_top3": ME._top_emotions(u, low), "sentiment_agent_top3": ME._top_emotions(a, low),
                    "user_scores": u.get("scores") or {}, "agent_scores": a.get("scores") or {}})
    W = 20
    flip_targets: Counter = Counter()
    for s in range(0, max(1, len(enr) - W)):
        win = enr[s:s + W]
        rvm: dict = {}
        for rule in rules:
            v = ME._rule_votes(rule, win, None)
            if v:
                rvm[rule["mood_id"]] = rvm.get(rule["mood_id"], 0) + v
        lvm, _ = ME._llm_mood_votes(win, moods, thr, wt)
        comb = dict(lvm)
        for k, v in rvm.items():
            comb[k] = comb.get(k, 0) + v
        if not comb:
            continue
        lw = max(lvm, key=lvm.get) if lvm else None
        cw = max(comb, key=comb.get)
        if cw != lw:
            flip_targets[cw] += 1

    out = []
    for mood, cnt in flip_targets.items():
        if cnt < min_flips or mood not in moods:
            continue
        emo_freq: Counter = Counter(); mutual = False; modes: set = set(); topics: set = set()
        for rule in rules:
            if rule["mood_id"] != mood:
                continue
            rt = rule["rule_type"]
            if rt in ("sentiment_agent", "sentiment_user", "sentiment_match", "sentiment_mismatch"):
                for e in (rule["params"].get("emotions") or []):
                    emo_freq[e] += 1
            if rt == "sentiment_match":
                mutual = True
            if rt in ("mode_count", "mode_streak") and rule["params"].get("mode"):
                modes.add(rule["params"]["mode"])
            if rt == "topic_keyword":
                for k in (rule["params"].get("keywords") or []):
                    topics.add(k)
        if not emo_freq:
            continue
        mx = max(emo_freq.values())
        emos = {e: round(c / mx, 2) for e, c in emo_freq.most_common(6)}
        out.append({
            "target_mood": mood, "agent_emotions": emos,
            "relation": "mutual" if mutual else "none", "user_emotions": {},
            "mode": sorted(modes), "topic_contains": sorted(topics), "strength": 0.6,
            "note": f"LLM under-scores {moods[mood]['name']} when "
                    f"{'/'.join(list(emos)[:3])} present ({cnt} flips)",
            "enabled": True, "_flips": cnt,
        })
    return out


async def seed_profile(profile_name: str, db_path: str | None = None, force: bool = False) -> list[dict]:
    """Derive + insert corrections for a profile if it has none (or force). Returns the seeded set."""
    existing = await list_corrections(profile_name)
    if existing and not force:
        return existing
    proposed = await derive_corrections(profile_name, db_path=db_path)
    for c in proposed:
        await create_correction(
            profile_name, c["target_mood"], c["agent_emotions"], relation=c["relation"],
            user_emotions=c["user_emotions"], mode=c["mode"], topic_contains=c["topic_contains"],
            strength=c["strength"], note=c["note"], enabled=c["enabled"])
    return await list_corrections(profile_name)
