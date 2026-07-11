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
from ..db.ego import get_ego_db

_COLS = ("id, profile_name, target_mood, agent_emotions, relation, user_emotions, mode, "
         "topic_contains, strength, note, enabled, created_at")

# Conversation modes available as the "undercurrent" trigger (mirrors the mood rule-builder).
CONVERSATION_MODES = ["work", "social", "informative", "serious", "flirting", "creative", "support"]

# Grouping for the emotion picker so it's scannable (unknown/extra taxonomy emotions → "Other").
_EMOTION_GROUP_MAP = {
    "Warm / positive": ["admiration", "affection", "amusement", "approval", "awe", "caring",
                        "contentment", "gratitude", "joy", "love", "optimism", "pride", "relief",
                        "tenderness", "trust"],
    "Desire / romantic": ["desire", "arousal", "lust", "horny", "yearning", "longing",
                          "infatuation", "passion", "possessiveness"],
    "Negative": ["anger", "annoyance", "boredom", "contempt", "disappointment", "disapproval",
                "disgust", "embarrassment", "fear", "grief", "jealousy", "loneliness",
                "nervousness", "remorse", "sadness"],
    "Cognitive / other": ["anticipation", "confusion", "curiosity", "excitement", "realization",
                          "surprise", "neutral", "approval"],
}


async def emotion_groups() -> dict:
    """{group: [emotion]} over the configured taxonomy, for the picker (extras → 'Other')."""
    from .settings_store import get_emotion_taxonomy
    tax = set(await get_emotion_taxonomy())
    groups: dict = {}
    placed: set = set()
    for grp, emos in _EMOTION_GROUP_MAP.items():
        picks = [e for e in emos if e in tax and e not in placed]
        if picks:
            groups[grp] = picks
            placed |= set(picks)
    extra = sorted(tax - placed)
    if extra:
        groups["Other"] = extra
    return groups


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
        "headroom": await _f("mood_correction_headroom", 20.0),
        "solo_positive_weight": await _f("mood_solo_positive_weight", 0.6),
        "solo_negative_weight": await _f("mood_solo_negative_weight", 0.3),
    }


def _solo_weight(rnd: dict, cfg: dict) -> float:
    """De-rating multiplier for a solo (sidequest) round: <1, and negatives weighted below positives so a
    flopped sidequest barely dents mood while a good one still lifts. 1.0 for normal conversation rounds."""
    if not rnd.get("solo"):
        return 1.0
    neg = float(rnd.get("valence", 0.0)) < 0
    return cfg.get("solo_negative_weight", 0.3) if neg else cfg.get("solo_positive_weight", 0.6)


def backbone_votes(enriched: list[dict], moods: dict, cfg: dict) -> dict:
    """Continuous backbone: recency-weighted sum of the local scorer's per-round mood scores (0–10,
    normalized to 0–1), unscaled. The caller applies backbone_scale."""
    hl = cfg["halflife"]
    out: dict = {}
    for i, rnd in enumerate(enriched):
        rec = 0.5 ** (i / hl) * _solo_weight(rnd, cfg)
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
    headroom = cfg.get("headroom", 20.0)
    # Gap-filling: a correction only fills the headroom between the backbone and a ceiling — so it fires
    # fully where the LLM under-reads a mood and fades to ~0 where the LLM already reads it high. Stops the
    # correction double-counting the same emotions the backbone already used (the self-reinforcement amp).
    # Applied in scaled (panel) units; per-correction contributions scale by the same per-mood factor so the
    # UI cards still sum to the (capped) per-mood total.
    tgt_of = {c["id"]: c["target_mood"] for c in corrs}
    cap_factor: dict = {}   # mood -> capped/raw ratio (1.0 = uncapped)
    cv_scaled: dict = {}
    for mid, raw in cv.items():
        raw_s = bs * raw
        capped_s = min(raw_s, max(0.0, headroom - bs * bb.get(mid, 0.0))) if headroom > 0 else raw_s
        cv_scaled[mid] = capped_s
        cap_factor[mid] = (capped_s / raw_s) if raw_s > 0 else 1.0
    vote_map: dict = {}
    for mid in set(bb) | set(cv):
        vote_map[mid] = round(bs * bb.get(mid, 0.0) + cv_scaled.get(mid, 0.0), 3)
    debug = {
        "backbone": {k: round(bs * v, 2) for k, v in bb.items()},
        "corrections": {k: round(v, 2) for k, v in cv_scaled.items()},
        "per_correction": {k: round(bs * v * cap_factor.get(tgt_of.get(k), 1.0), 2)
                           for k, v in per_corr.items()},
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
            # newest round (i=0) weight 1.0, halving every `halflife` rounds; solo rounds de-rated (asymmetric)
            rec = 0.5 ** (i / halflife) * _solo_weight(rnd, cfg)
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


# --- The corrective-layer view (UI data: Now/Why, backbone-vs-corrections, Gaps, shadow) ---

def _narrative(current: dict | None, bb_ranked: list, net_ranked: list, corrs: list, moods: dict) -> str:
    """Deterministic one-liner from the structured state — no LLM, truthful by construction."""
    parts = [f"Currently **{current['name']}**." if current else "No mood set."]
    if bb_ranked:
        parts.append(f"The LLM reads {bb_ranked[0]['name']} strongest ({bb_ranked[0]['votes']:.0f})")
        if len(bb_ranked) > 1:
            parts[-1] += f", then {bb_ranked[1]['name']} ({bb_ranked[1]['votes']:.0f})."
        else:
            parts[-1] += "."
    if net_ranked and bb_ranked and net_ranked[0]["mood"] != bb_ranked[0]["mood"]:
        parts.append(f"With your corrections, {net_ranked[0]['name']} is top ({net_ranked[0]['net']:.0f}).")
    firing = [c for c in corrs if c.get("firing")]
    if firing:
        parts.append("Corrections firing: " + ", ".join(
            f"{moods[c['target_mood']]['name']} +{c['contribution']:.1f}" for c in firing) + ".")
    else:
        parts.append("No corrections are firing right now.")
    return " ".join(parts)


async def _compute_gaps(profile_name: str, enr: list, moods: dict, corrs: list,
                        backbone: dict, vote_map: dict, headroom: float = 20.0) -> list:
    """Deterministic gap analysis (no LLM), each ACTIONABLE:
      • dead     — a correction that isn't firing.
      • miss     — the legacy rules read a mood in recent rounds but the LLM backbone isn't ranking it
                   (the rules are the 'should-have' reference during the shadow trial). Names the mood +
                   emotions to correct with. This is the one that answers "did the LLM miss a mood?".
      • cascade  — a correction's target exceeds a cascade threshold and escalates away.
    """
    from .settings_store import get_mood_cascade
    from . import mood_engine as ME
    gaps = []
    corrected = {c["target_mood"] for c in corrs}

    # dead corrections — distinguish "the LLM already has it" (gap-fill standing down, working as intended)
    # from "genuinely idle" (the emotions it looks for aren't present).
    for c in corrs:
        if c.get("enabled") and c.get("contribution", 0.0) <= 0.05:
            name = moods[c["target_mood"]]["name"]
            if backbone.get(c["target_mood"], 0.0) >= 0.9 * headroom:
                gaps.append({"kind": "dead", "text":
                    f"Your {name} correction is standing down — the LLM already reads {name} strongly "
                    f"(near the ceiling), so the correction doesn't pile on. Working as intended."})
            else:
                gaps.append({"kind": "dead", "text":
                    f"Your {name} correction isn't firing — the emotions it looks for aren't in recent rounds."})

    # cascade escapes
    casc_enabled, cascade = await get_mood_cascade()
    if not casc_enabled:
        cascade = {}
    for c in corrs:
        m = c["target_mood"]
        if m in cascade and vote_map.get(m, 0) >= cascade[m].get("at", 99):
            tgt = cascade[m].get("to")
            gaps.append({"kind": "cascade", "text":
                f"{moods[m]['name']} ({vote_map[m]:.0f}) exceeds its cascade threshold → escalates into "
                f"{moods.get(tgt, {}).get('name', tgt)}; this correction's boost may be funneled away."})
    return gaps


async def _mood_history_rows(profile_name: str, moods: dict, limit: int = 30) -> list:
    """Recent mood *changes* (newest-first), names/colors resolved — the long-term change log."""
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT prev_mood_id, mood_id, vote_count, changed_at FROM mood_history "
            "WHERE profile_name = ? ORDER BY changed_at DESC LIMIT ?", (profile_name, limit))
        rows = await cur.fetchall()
    finally:
        await conn.close()
    def _m(mid):
        return {"name": moods.get(mid, {}).get("name", "—"), "color": moods.get(mid, {}).get("color", "#888")}
    return [{"prev": _m(r[0]) if r[0] else None, "mood": _m(r[1]), "votes": r[2], "at": r[3]} for r in rows]


async def round_history(profile_name: str, db_path: str | None = None, limit: int = 60) -> list:
    """Per-round scoring history over the retained window (newest-first): what the LLM read that round
    (persisted `mood_scores`), the agent's top emotions, the mode, and the mood in effect at that time
    (reconstructed from mood_history). Bounded by retention_days — rounds are pruned with old conversations.
    """
    import datetime as _dt
    from . import mood_engine as ME
    from .conversations import get_recent_rounds
    moods = await ME._load_moods()
    rounds = await get_recent_rounds(profile_name, limit=limit)
    if not rounds:
        return []
    round_ids = [r["id"] for r in rounds]
    conv_ids = list({r["conversation_id"] for r in rounds})
    sentiment_map, mood_scores_map, _topic, mode_map = await ME._fetch_round_enrichment(round_ids, conv_ids)

    # Mood in effect at a round = mood_id of the latest change with changed_at <= end_ts (ascending walk).
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT mood_id, prev_mood_id, changed_at FROM mood_history WHERE profile_name = ? "
            "ORDER BY changed_at ASC", (profile_name,))
        changes = await cur.fetchall()
    finally:
        await conn.close()

    def _mood_at(ts):
        m = changes[0][1] if changes else None  # mood before the earliest recorded change
        for mid, _prev, at in changes:
            if at <= ts:
                m = mid
            else:
                break
        return m

    out = []
    for r in rounds:
        ts = r.get("end_ts") or 0
        ms = mood_scores_map.get(r["id"]) or {}
        reads = sorted(((mid, float(v)) for mid, v in ms.items() if mid in moods and float(v) > 0),
                       key=lambda x: -x[1])[:3]
        ascores = ((sentiment_map.get(r["id"]) or {}).get("agent") or {}).get("scores") or {}
        emos = sorted(((e, round(float(v), 2)) for e, v in ascores.items()
                       if e not in ("neutral", "approval") and float(v) >= 0.2), key=lambda x: -x[1])[:5]
        amid = _mood_at(ts)
        out.append({
            "when": _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "",
            "active_mood": {"name": moods.get(amid, {}).get("name", "—"),
                            "color": moods.get(amid, {}).get("color", "#888")} if amid else None,
            "reads": [{"name": moods[mid]["name"], "color": moods[mid].get("color", "#888"),
                       "score": round(sc, 1)} for mid, sc in reads],
            "emotions": [{"e": e, "v": v} for e, v in emos],
            "mode": mode_map.get(r["conversation_id"]),
        })
    return out


async def mood_config_rows(profile_name: str, db_path: str | None = None) -> list:
    """Per-mood resolution config (resting membership + activation threshold) with the current backbone
    read, for the v2-page editor. Sorted by backbone desc so the moods in play are on top."""
    from . import mood_engine as ME
    from .profiles import resolve_profile
    db_path = db_path or resolve_profile(profile_name)
    moods = await ME._load_moods()
    enr = await ME._build_round_enriched(profile_name, db_path)
    _vm, dbg = await v2_vote_map(profile_name, enr, moods)
    backbone = dbg.get("backbone", {})
    defaults = set(await ME._load_defaults(profile_name, moods))
    thresholds = await ME._load_thresholds(profile_name)
    rows = [{
        "id": mid, "name": m["name"], "color": m.get("color", "#888"),
        "resting": mid in defaults,
        "threshold": thresholds.get(mid, m.get("min_votes", 1)),
        "backbone": round(backbone.get(mid, 0.0), 1),
    } for mid, m in moods.items()]
    rows.sort(key=lambda r: (-r["backbone"], r["name"]))
    return rows


async def mood_summary(profile_name: str, db_path: str | None = None) -> dict:
    """Light, read-only mood snapshot for the dashboard panel: current mood, a pending recompute (only
    when it differs), the top net signals, recent transitions, and a short 'why'. Reuses the same pieces
    as the mood page but skips the heavy editor/gaps/rounds data. Does NOT commit — the scheduled
    refresh_all_moods owns writes; opening the dashboard shouldn't move the mood."""
    from . import mood_engine as ME
    from .profiles import resolve_profile
    from .settings_store import get_mood_decay_config, get_transition_config
    db_path = db_path or resolve_profile(profile_name)
    moods = await ME._load_moods()
    current = await ME.get_cached_mood(profile_name)
    enr = await ME._build_round_enriched(profile_name, db_path)

    net_top, pending, shaping = [], None, {}
    if enr and moods:
        vote_map, dbg = await v2_vote_map(profile_name, enr, moods)
        backbone = dbg.get("backbone", {}); corr_scaled = dbg.get("corrections", {})
        net_top = sorted(
            ({"name": moods[m]["name"], "color": moods[m].get("color", "#888"),
              "net": round(backbone.get(m, 0.0) + corr_scaled.get(m, 0.0), 1)}
             for m in (set(backbone) | set(corr_scaled)) if m in moods),
            key=lambda x: -x["net"])[:4]
        cached = await ME._load_cached_mood(profile_name)
        thresholds = await ME._load_thresholds(profile_name)
        winner = await ME._resolve_mood(profile_name, dict(vote_map), [], moods, cached, thresholds, commit=False)
        if winner and (not current or winner["id"] != current["id"]):
            pending = {"name": winner["name"], "color": winner.get("color", "#888"),
                       "vote_count": winner.get("vote_count", 0)}
        tcfg = await get_transition_config(); decay_cfg = await get_mood_decay_config()
        tenure = await ME._mood_tenure(profile_name)
        shaping = {"tenure": tenure, "bias": ME._incumbent_bias(tenure, tcfg, decay_cfg)}
    recent_changes = await _mood_history_rows(profile_name, moods, limit=5)
    return {"current": current, "pending": pending, "net_top": net_top,
            "recent_changes": recent_changes, "shaping": shaping}


async def corrective_view(profile_name: str, db_path: str | None = None) -> dict:
    """Assemble everything the corrective-layer UI (the mood page) needs."""
    from . import mood_engine as ME
    from .profiles import resolve_profile
    from ..config import settings as _cfg_settings
    db_path = db_path or resolve_profile(profile_name)
    moods = await ME._load_moods()
    enr = await ME._build_round_enriched(profile_name, db_path)
    cfg = await get_correction_config()

    vote_map, dbg = await v2_vote_map(profile_name, enr, moods, cfg)
    backbone = dbg.get("backbone", {}); per_corr = dbg.get("per_correction", {})
    bb_ranked = sorted(
        ({"mood": m, "name": moods[m]["name"], "votes": backbone[m]} for m in backbone if m in moods),
        key=lambda x: -x["votes"])[:8]
    # Net ranking = backbone + corrections (what actually feeds the shaping layer; "top" refers to this).
    corr_scaled = dbg.get("corrections", {})
    net_ranked = sorted(
        ({"mood": m, "name": moods[m]["name"],
          "backbone": round(backbone.get(m, 0.0), 1), "correction": round(corr_scaled.get(m, 0.0), 1),
          "net": round(backbone.get(m, 0.0) + corr_scaled.get(m, 0.0), 1)}
         for m in (set(backbone) | set(corr_scaled)) if m in moods),
        key=lambda x: -x["net"])[:8]

    cached = await ME._load_cached_mood(profile_name)
    thresholds = await ME._load_thresholds(profile_name)
    v2_winner = await ME._resolve_mood(profile_name, dict(vote_map), [], moods, cached, thresholds, commit=False)
    current = await ME.get_cached_mood(profile_name)

    corrs = await list_corrections(profile_name)
    for c in corrs:
        c["contribution"] = round(per_corr.get(c["id"], 0.0), 2)
        c["firing"] = c["contribution"] > 0.05
        c["target_name"] = moods.get(c["target_mood"], {}).get("name", c["target_mood"])

    # Shaping visibility — what the cascade/bias/cooldown layer does AFTER the scores (why the winner
    # can differ from the top LLM read).
    from .settings_store import get_mood_decay_config, get_transition_config, get_mood_cascade
    decay_cfg = await get_mood_decay_config(); tcfg = await get_transition_config()
    casc_enabled, cascade = await get_mood_cascade()
    if not casc_enabled:
        cascade = {}
    cooldown = await ME._cooldown_excluded(profile_name, decay_cfg, cascade)
    tenure = await ME._mood_tenure(profile_name)
    bias = ME._incumbent_bias(tenure, tcfg, decay_cfg)
    winner_id = v2_winner["id"] if v2_winner else None
    net_top = net_ranked[0]["mood"] if net_ranked else None
    note = ""
    if winner_id and net_top and net_top != winner_id:
        casc_to = cascade.get(net_top, {}).get("to")
        if net_top in cooldown:
            note = f"{moods[net_top]['name']} is the net top but on cooldown — {moods[winner_id]['name']} wins instead."
        elif casc_to == winner_id:
            note = f"{moods[net_top]['name']} (net top) cascades into {moods[winner_id]['name']} — that's why it wins."
        elif casc_to in cooldown:
            note = (f"{moods[net_top]['name']} (net top) cascades into {moods.get(casc_to, {}).get('name', casc_to)}, "
                    f"which is on cooldown — its votes are lost, so {moods[winner_id]['name']} wins.")
        elif winner_id == cached and bias > 0:
            note = f"{moods[winner_id]['name']} is held on inertia (+{bias})."
        else:
            note = f"{moods[winner_id]['name']} wins after cascade/bias reshaping."
    shaping = {"cooldown": [moods[m]["name"] for m in cooldown if m in moods],
               "tenure": tenure, "bias": bias, "note": note}

    # Round history — per-round LLM read + emotions + mode + the mood in effect then (over the retained
    # window), and the long-term mood-change log. Both formerly only on the legacy rules page.
    import datetime as _dt
    corr_emos: set = set()
    for c in corrs:
        corr_emos |= set((c.get("agent_emotions") or {}).keys())
    rounds_hist = await round_history(profile_name, db_path=db_path)
    for row in rounds_hist:
        for em in row["emotions"]:
            em["corr"] = em["e"] in corr_emos
    mood_hist = await _mood_history_rows(profile_name, moods)
    mood_config = await mood_config_rows(profile_name, db_path=db_path)

    gaps = await _compute_gaps(profile_name, enr, moods, corrs, backbone, vote_map, cfg.get("headroom", 20.0))

    # Exit triggers (directed abrupt transitions)
    from . import mood_exits as _MX
    cur_id = current["id"] if current else None
    exits = await _MX.list_exits(profile_name)
    for e in exits:
        e["source_name"] = moods.get(e["source_mood"], {}).get("name") if e["source_mood"] else "any mood"
        e["target_name"] = moods.get(e["target_mood"], {}).get("name", e["target_mood"])
        e["armed"] = (e["source_mood"] is None) or (e["source_mood"] == cur_id)
        if e["condition_type"] == "signal":
            c = e["condition"]
            e["cond_summary"] = f"{c.get('metric')} {c.get('op')} {c.get('value')}"
        else:
            e["cond_summary"] = e["condition"].get("text", "")
    signals = [{"name": k, "label": v["label"], "unit": v["unit"]} for k, v in _MX.SIGNALS.items()]

    return {
        "profile": profile_name, "rounds": len(enr),
        "current": current, "v2_winner": v2_winner,
        "backbone": bb_ranked, "net_ranked": net_ranked, "corrections": corrs, "gaps": gaps,
        "shaping": shaping, "rounds_hist": rounds_hist, "mood_hist": mood_hist,
        "mood_config": mood_config, "retention_days": _cfg_settings.retention_days,
        "narrative": _narrative(current, bb_ranked, net_ranked, corrs, moods),
        "config": cfg,
        "all_moods": sorted(({"id": m, "name": moods[m]["name"]} for m in moods), key=lambda x: x["name"]),
        "all_modes": CONVERSATION_MODES,
        "emotion_groups": await emotion_groups(),
        "exits": exits, "signals": signals, "signal_ops": ["<", "<=", ">", ">=", "=="],
    }
