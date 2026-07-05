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


# --- The corrective-layer view (UI data: Now/Why, backbone-vs-corrections, Gaps, shadow) ---

def _narrative(current: dict | None, bb_ranked: list, corrs: list, moods: dict) -> str:
    """Deterministic one-liner from the structured state — no LLM, truthful by construction."""
    parts = [f"Currently **{current['name']}**." if current else "No mood set."]
    if bb_ranked:
        parts.append(f"The LLM reads {bb_ranked[0]['name']} strongest ({bb_ranked[0]['votes']:.0f})")
        if len(bb_ranked) > 1:
            parts[-1] += f", then {bb_ranked[1]['name']} ({bb_ranked[1]['votes']:.0f})."
        else:
            parts[-1] += "."
    firing = [c for c in corrs if c.get("firing")]
    if firing:
        parts.append("Corrections firing: " + ", ".join(
            f"{moods[c['target_mood']]['name']} +{c['contribution']:.1f}" for c in firing) + ".")
    else:
        parts.append("No corrections are firing right now.")
    return " ".join(parts)


async def _compute_gaps(profile_name: str, enr: list, moods: dict, corrs: list,
                        backbone: dict, vote_map: dict) -> list:
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

    # dead corrections
    for c in corrs:
        if c.get("enabled") and c.get("contribution", 0.0) <= 0.05:
            gaps.append({"kind": "dead", "text":
                f"Your {moods[c['target_mood']]['name']} correction isn't firing — the emotions it looks "
                f"for aren't in recent rounds."})

    # miss: the legacy rules flip the winner away from the LLM's pick — the live version of the flip
    # analysis that seeded the corrections. If the rules override the LLM toward a mood no correction
    # covers, that's a candidate correction (names the mood + the emotions the rules used).
    try:
        rules = [r for r in await ME._load_rules(profile_name) if r["rule_type"] != "prev_mood"]
        rule_votes: dict = {}; rule_emos: dict = {}
        for rule in rules:
            v = ME._rule_votes(rule, enr, None)
            if not v:
                continue
            rule_votes[rule["mood_id"]] = rule_votes.get(rule["mood_id"], 0) + v
            if rule["rule_type"] in ("sentiment_agent", "sentiment_user", "sentiment_match"):
                rule_emos.setdefault(rule["mood_id"], Counter())
                for e in (rule["params"].get("emotions") or []):
                    rule_emos[rule["mood_id"]][e] += 1
        enabled, thr_llm, wt = await ME._llm_vote_config()
        llm_only, _ = ME._llm_mood_votes(enr, moods, thr_llm, wt) if enabled else ({}, [])
        combined = dict(llm_only)
        for mid, v in rule_votes.items():
            combined[mid] = combined.get(mid, 0) + v
        if combined and llm_only:
            legacy_top = max(combined, key=combined.get)
            llm_top = max(llm_only, key=llm_only.get)
            if legacy_top != llm_top and legacy_top in moods and legacy_top not in corrected:
                emos = [e for e, _ in rule_emos.get(legacy_top, Counter()).most_common(3)]
                frm = f" (from {', '.join(emos)})" if emos else ""
                gaps.append({"kind": "miss", "text":
                    f"The rules pull the read toward {moods[legacy_top]['name']}{frm}, over the LLM's "
                    f"{moods[llm_top]['name']} — a {moods[legacy_top]['name']} correction would capture that.",
                    "suggest_mood": legacy_top, "suggest_emotions": emos})
    except Exception:
        pass

    # cascade escapes
    _, cascade = await get_mood_cascade()
    for c in corrs:
        m = c["target_mood"]
        if m in cascade and vote_map.get(m, 0) >= cascade[m].get("at", 99):
            tgt = cascade[m].get("to")
            gaps.append({"kind": "cascade", "text":
                f"{moods[m]['name']} ({vote_map[m]:.0f}) exceeds its cascade threshold → escalates into "
                f"{moods.get(tgt, {}).get('name', tgt)}; this correction's boost may be funneled away."})
    return gaps


async def _shadow_stats(profile_name: str) -> dict:
    conn = await get_ego_db()
    try:
        cur = await conn.execute(
            "SELECT value FROM module_data WHERE module='mood_shadow' AND key=?", (profile_name,))
        row = await cur.fetchone()
    finally:
        await conn.close()
    if not row:
        return {"total": 0, "agree": 0, "disagreements": []}
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return {"total": 0, "agree": 0, "disagreements": []}


async def corrective_view(profile_name: str, db_path: str | None = None) -> dict:
    """Assemble everything the corrective-layer UI needs, for the shadow trial."""
    from . import mood_engine as ME
    from .settings_store import get_setting
    from .profiles import resolve_profile
    db_path = db_path or resolve_profile(profile_name)
    moods = await ME._load_moods()
    enr = await ME._build_round_enriched(profile_name, db_path)
    cfg = await get_correction_config()
    mode = await get_setting("mood_scoring_mode", "legacy")

    vote_map, dbg = await v2_vote_map(profile_name, enr, moods, cfg)
    backbone = dbg.get("backbone", {}); per_corr = dbg.get("per_correction", {})
    bb_ranked = sorted(
        ({"mood": m, "name": moods[m]["name"], "votes": backbone[m]} for m in backbone if m in moods),
        key=lambda x: -x["votes"])[:8]

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
    _, cascade = await get_mood_cascade()
    cooldown = await ME._cooldown_excluded(profile_name, decay_cfg, cascade)
    tenure = await ME._mood_tenure(profile_name)
    bias = ME._incumbent_bias(tenure, tcfg, decay_cfg)
    ranked = sorted(vote_map, key=vote_map.get, reverse=True)
    winner_id = v2_winner["id"] if v2_winner else None
    barred_higher = [moods[m]["name"] for m in ranked
                     if m in cooldown and m in moods
                     and (winner_id is None or vote_map[m] > vote_map.get(winner_id, 0))]
    top_raw = ranked[0] if ranked else None
    note = ""
    if winner_id and top_raw and top_raw != winner_id:
        if barred_higher:
            note = f"{', '.join(barred_higher)} outscore {moods[winner_id]['name']} but are on cooldown — so it wins by elimination."
        elif top_raw in cascade and cascade[top_raw].get("to") in cooldown:
            tgt = cascade[top_raw]["to"]
            note = (f"{moods[top_raw]['name']} (top) cascades into {moods.get(tgt, {}).get('name', tgt)}, "
                    f"which is on cooldown — so its votes are lost and {moods[winner_id]['name']} wins.")
        elif winner_id == cached and bias > 0:
            note = f"{moods[winner_id]['name']} is held on inertia (+{bias})."
        else:
            note = f"{moods[winner_id]['name']} wins after shaping (cascade/bias)."
    shaping = {"cooldown": [moods[m]["name"] for m in cooldown if m in moods],
               "tenure": tenure, "bias": bias, "note": note}

    # Per-round emotion detail — what the corrections are reacting to (helps tune weights/strength).
    import datetime as _dt
    corr_emos: set = set()
    for c in corrs:
        corr_emos |= set((c.get("agent_emotions") or {}).keys())
    rounds_detail = []
    for rnd in enr[:10]:
        sc = rnd.get("agent_scores") or {}
        tops = sorted(((e, round(float(v), 2)) for e, v in sc.items()
                       if e not in ("neutral", "approval") and float(v) >= 0.2), key=lambda x: -x[1])[:6]
        rounds_detail.append({
            "when": _dt.datetime.fromtimestamp(rnd.get("end_ts", 0)).strftime("%m-%d %H:%M") if rnd.get("end_ts") else "",
            "mode": rnd.get("mode"),
            "emotions": [{"e": e, "v": v, "corr": e in corr_emos} for e, v in tops],
        })

    gaps = await _compute_gaps(profile_name, enr, moods, corrs, backbone, vote_map)
    shadow = await _shadow_stats(profile_name)
    if shadow.get("disagreements"):
        import datetime as _dt
        for d in shadow["disagreements"]:
            d["when"] = _dt.datetime.fromtimestamp(d.get("at", 0)).strftime("%m-%d %H:%M") if d.get("at") else ""

    return {
        "profile": profile_name, "mode": mode, "rounds": len(enr),
        "current": current, "v2_winner": v2_winner,
        "backbone": bb_ranked, "corrections": corrs, "gaps": gaps, "shadow": shadow,
        "shaping": shaping, "rounds_detail": rounds_detail,
        "narrative": _narrative(current, bb_ranked, corrs, moods),
        "config": cfg,
    }


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
