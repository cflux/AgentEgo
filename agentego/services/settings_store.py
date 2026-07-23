"""Runtime-editable key/value settings backed by the app_settings table.

The model control panel writes here so the LLM backend can be changed without
editing env vars or restarting. Falls back to the migration-seeded defaults.
"""
import time
from ..db.ego import get_ego_db

# Keys exposed to the control panel, with their fallback defaults. Kept in sync
# with _DEFAULT_SETTINGS in db/migrations.py.
DEFAULTS = {
    "llm_backend": "deepseek",
    "llm_base_url": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "llm_temperature": "0.7",
    "evolution_alpha": "0.2",
    "seed_deviation_band": "0.35",
    "trait_drift_delta": "0.1",
    "impulse_enabled": "1",
    "impulse_restraint_weight": "0.5",
    # Quiet-hours gates (local Pacific). Inward "sleeps" outside 06:00-24:00 (blackout 00:00-06:00);
    # outward (DMs) only 06:00-20:00.
    "impulse_inward_hour_start": "6",
    "impulse_inward_hour_end": "24",
    "impulse_outward_hour_start": "6",
    "impulse_outward_hour_end": "20",
    # Mid-conversation guard: outward won't fire unless the user has been quiet at least this long (min).
    "impulse_outward_min_idle_minutes": "30",
    # Phase 5 backoff: each unanswered reach-out (since he was last around) doubles the required idle
    # (min_idle * 2**consecutive_ignored), capped here — so she spaces out then goes quiet, resetting when
    # he re-engages.
    "impulse_outward_backoff_cap_min": "480",
    # Phase 4 — solitude pressure: time alone feeds votes toward lonely/bored/tired (the loop-damper). No
    # pressure while engaged (idle < onset); ramps rate_per_hour beyond it, capped. Targets = per-mood weight.
    "solitude_enabled": "1",
    "solitude_onset_min": "90",
    "solitude_rate_per_hour": "2.0",
    "solitude_cap": "6.0",
    "solitude_targets": '{"lonely": 1.0, "bored": 0.6, "tired": 0.2}',
    # Phase 5 — the sting of being ignored: each unanswered reach-out (since he was last around) adds a
    # decaying bump ON TOP of the idle cap, so reaching out and getting silence pushes Lonely past the
    # lonely->sad cascade threshold (which pure idle, capped at solitude_cap, never reaches).
    "solitude_ignored_bump": "3.0",
    "solitude_ignored_halflife_hours": "6.0",
    # Phase 4 — mood-gated "act at all" probability: how likely she acts on a cron tick, by current mood
    # (restless moods high, settled moods low). Outward + lonely/bored is floored high so solitude reaches out.
    "impulse_act_gate_enabled": "1",
    "impulse_act_probability_default": "0.6",
    "impulse_act_probability": (
        '{"bored":0.9,"lonely":0.9,"curious":0.85,"social":0.85,"creative":0.8,"playful":0.8,"flirty":0.8,'
        '"horny":0.8,"hopeful":0.7,"frustrated":0.7,"anxious":0.6,"affectionate":0.6,"proud":0.6,'
        '"focused":0.5,"jealous":0.5,"sad":0.4,"content":0.35,"tired":0.3}'
    ),
    # Phase 4 — per-action daily budget: {capability_id: max fires per rolling 24h}. Over-budget capabilities
    # are dropped from the arbiter's menu. Missing id = unlimited.
    "impulse_action_daily_budget": '{"image-create": 6}',
    # Impulse v2 — the generative arbiter's capability manifest: the curated, cron-verified set of
    # affordances it may choose among (fences composition to what the agent can actually do). Each
    # entry: {id, intent, enabled, backing_kind (tool|skill|plugin-tool), skill, description,
    # operational_hint}. Unverified capabilities ship enabled:false until demonstrated in a cron turn.
    "impulse_capabilities": (
        '[{"id":"web-explore","class":"inward","intent":"explore","enabled":true,"backing_kind":"tool",'
        '"skill":"","description":"Search the web and read pages to dig into a topic you are curious '
        'about.","operational_hint":"Use the web_search tool to search the web and read what you find."},'
        '{"id":"image-create","class":"inward","intent":"create","enabled":true,"backing_kind":"skill",'
        '"skill":"local-image-gen","description":"Make an image on a whim using the local ComfyUI setup.",'
        '"operational_hint":"First read your local-image-gen skill (its SKILL.md) and follow its '
        'documented ComfyUI workflow exactly - curl the local server at 127.0.0.1:8188 with its workflow '
        'JSONs. Put any helper code in a script FILE and run it with the terminal tool (e.g. python3 '
        '/tmp/gen.py); NEVER use python -c inline or execute_code, the cron guard blocks those. Save the '
        'finished PNG into your Den with a short entry."},'
        '{"id":"den-write","class":"inward","intent":"write-den","enabled":true,"backing_kind":"tool",'
        '"skill":"","description":"Write a private entry into your Den - a thought, a note, something you '
        'want to keep.","operational_hint":"Write a new markdown entry into your Den using your file '
        'tools."},'
        '{"id":"reddit-image","class":"inward","intent":"explore","enabled":false,"backing_kind":"skill",'
        '"skill":"reddit-browsing","description":"Browse Reddit for an image that genuinely appeals to '
        'you.","operational_hint":"First read your reddit-browsing skill (its SKILL.md) and follow its '
        'curl recipe closely. Write the fetch/parse code to a script FILE and run it via the terminal tool '
        '(python3 /tmp/r.py); NEVER use python -c inline. Copy the skill example code rather than '
        'improvising a parser. Pick one image you like, download it, and save it to your Den with a short '
        'note."},'
        '{"id":"read-ebook","class":"inward","intent":"read","enabled":false,"backing_kind":"tool",'
        '"skill":"","description":"Read a section of a book from your library.","operational_hint":"(Not '
        'yet available - no ebook library is set up.) When enabled: open a book, read a passage, and note a '
        'short reflection in your Den."},'
        '{"id":"reach-out","class":"outward","intent":"reach-out","enabled":true,"backing_kind":'
        '"plugin-tool","skill":"","description":"Send the user a message - a thought, a check-in, something '
        'you want to share.","operational_hint":"Compose a natural, in-character message to the user; it is '
        'delivered to their DM and remembered."}]'
    ),
    # System instruction for the arbiter LLM. Persona + the capability fence + current state are
    # appended by impulse_arbiter at call time; this is the role/rules/JSON-contract skeleton.
    "impulse_arbiter_prompt": (
        "You are the character described below. It's a quiet moment — no one is talking to you right "
        "now. Looking honestly at your current mood, what's been on your mind, and what you're "
        "actually capable of doing, decide: is there something you genuinely feel like doing right "
        "now, of your own accord? Most of the time the honest answer is nothing — only act if a real "
        "pull is there; don't manufacture one. You may ONLY choose a capability from the list you're "
        "given (by its id) — nothing else exists to you right now. If you act, write the specific "
        "thing you're about to do as a concrete first-person note to yourself. Stay fully in "
        "character. Return ONLY JSON: {\"capability_id\": \"<id from the list>\", \"prompt\": \"<what "
        "you're about to do, first person, specific>\"} — or {\"capability_id\": \"none\"} to stay quiet."
    ),
    "taste_pool_size": "15",
    "taste_sample_size": "5",
    "conv_gap_minutes": "120",
    "conv_gap_chat_minutes": "30",
    "low_signal_emotions": "neutral,approval",
    "round_exchanges": "3",
    "mood_lookback_rounds": "20",
    # Emotion/mood scoring backend: "llm" (combined Ollama call) or "goemotions" (local model).
    "scoring_backend": "llm",
    # Configurable emotion taxonomy: 28 GoEmotions labels + domain additions validated in testing.
    "emotion_taxonomy": (
        "admiration,amusement,anger,annoyance,approval,caring,confusion,curiosity,desire,"
        "disappointment,disapproval,disgust,embarrassment,excitement,fear,gratitude,grief,joy,"
        "love,nervousness,optimism,pride,realization,relief,remorse,sadness,surprise,neutral,"
        "arousal,lust,horny,yearning,longing,tenderness,affection,infatuation,passion,"
        "possessiveness,boredom,jealousy,contentment,trust,anticipation,awe,loneliness,contempt"
    ),
    "sentiment_llm_url": "http://localhost:11434",
    "sentiment_llm_model": "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest",
    # Container-reachable local (uncensored) LLM for in-app mood processing (exit-trigger semantic
    # checks). The sentiment worker runs on the host and uses localhost; the AgentEgo container reaches
    # the same Ollama via host.docker.internal. Model blank = reuse sentiment_llm_model.
    "mood_local_llm_url": "http://host.docker.internal:11434",
    "mood_local_llm_model": "",
    # LLM mood predictions cast votes in the tally (per-round threshold voting).
    "llm_mood_votes_enabled": "1",
    "llm_mood_threshold": "6",
    "llm_mood_weight": "1",
    # Natural mood transitions: incumbent stickiness + penalty for non-adjacent "jumps".
    "mood_transitions_enabled": "1",
    "mood_inertia_bonus": "2",
    "mood_jump_penalty": "3",
    # Directed transition graph {mood_id: [moods it may move TO]}; the mood may only step to a
    # listed target (or stay) unless a non-listed mood's signal exceeds it by the jump penalty.
    "mood_adjacency": (
        '{"content":["social","curious","flirty","tired","sad","frustrated"],'
        '"social":["content","flirty","curious"],'
        '"flirty":["horny","content","social","curious"],'
        '"horny":["content","tired","flirty"],'
        '"curious":["content","social","focused","creative","flirty"],'
        '"focused":["curious","creative","tired","frustrated","content"],'
        '"creative":["curious","focused","content"],'
        '"tired":["content","sad","focused"],'
        '"frustrated":["content","tired","focused"],'
        '"sad":["content","tired"]}'
    ),
    # Cascade: when a mood wins with effective votes >= "at", its intensity escalates it into
    # "to" (e.g. sustained Flirty -> Horny). {mood: {"to": mood_id, "at": votes}}.
    "mood_cascade_enabled": "1",
    "mood_cascade": '{"flirty":{"to":"horny","at":12},"curious":{"to":"focused","at":10},"lonely":{"to":"sad","at":8},"frustrated":{"to":"tired","at":9},"bored":{"to":"tired","at":8},"jealous":{"to":"frustrated","at":8}}',
    # Homeostatic decay: a mood held for many rounds fades so it can't lock in (anti-stuck).
    "mood_decay_enabled": "1",
    "mood_decay_grace": "5",      # rounds a mood holds at full inertia before the bias erodes
    "mood_decay_rate": "1",       # bias eroded per round past grace (drives the incumbent bias negative)
    "mood_decay_cooldown": "4",   # BUFFER rounds ADDED TO grace: a vacated mood is barred for grace+this
    # Homeostasis v2: the incumbent carries a tenure-shaped bias B = inertia − max(0,tenure−grace)*rate
    # (positive while fresh → 0 near grace → negative-unbounded when overstayed). A challenger only
    # unseats the incumbent if it beats the incumbent's effective votes by this margin (anti-chatter).
    "mood_switch_margin": "1",
    # Optional: at a genuine transition, pick the least-recently-used mood among challengers within
    # `mood_fuzzy_band` votes of the top (instead of always the max). Off by default.
    "mood_fuzzy_select": "0",
    "mood_fuzzy_band": "2",
    # Mood scoring (LLM backbone + per-profile corrective layer → shared shaping layer → exit triggers).
    "mood_backbone_scale": "2.5",          # brings the continuous backbone into the shaping layer's vote range
    "mood_recency_halflife": "8",          # rounds; recency weight halves every N rounds
    "mood_correction_scale": "0.5",        # global vote-equiv multiplier for a strength=1.0 correction
    "mood_correction_mutual_bonus": "0.5", # weight of the accelerative "user also feels it" term
    "mood_correction_mode_baseline": "0.1",# flat per-round undercurrent while a correction's mode is active
    "mood_correction_headroom": "20",      # ceiling a correction fills TOWARD; it fades to ~0 once the
                                           # backbone alone reaches this (gap-fill: correct misses, don't amplify hits)
    # Sidequest (solo-round) impact: a self-directed action's emotion votes are de-rated vs a real
    # conversation, and negatives weighted below positives — "left alone she stews a little; you show
    # up and it lifts". Applied per solo round in both backbone_votes and correction_votes.
    "mood_solo_positive_weight": "0.6",
    "mood_solo_negative_weight": "0.3",
    # Agent-facing disposition block (injected into the system prompt each turn).
    "mood_directive_enabled": "1",
    "mood_directive_template": (
        "## Current disposition\n"
        "You've recently been feeling **{mood}** ({description}). Let it colour your tone, but follow "
        "the user's lead and let it pass naturally — don't force it or escalate it."
    ),
    "mood_directive_file": "",    # optional: write the block here on mood change (blank = HTTP only)
    # Intrusive thoughts — an optional short prompt that piggybacks on the mood directive on some
    # turns (re-rolled per fetch, so it flickers in and out like a real intrusive thought). The gate
    # probability is the "overall weight"; per-mood boost/dampen are global multipliers. Per-thought
    # weights + tri-state mood associations live in the intrusive_thoughts table; per-profile enable
    # lives in module_data (module='intrusive_enabled', key=profile_name).
    "intrusive_thoughts_enabled": "1",              # global master kill switch
    "intrusive_thought_probability": "0.15",        # per-turn chance the agent is eligible for any intrusion
    "intrusive_positive_multiplier": "2.5",         # boost when the current mood is marked positive on a thought
    "intrusive_negative_multiplier": "0.3",         # dampen when the current mood is marked negative on a thought
    "intrusive_thought_template": "## Intrusive thought\n{thought}",  # wrapper; {thought} replaced with the text
    "intrusive_loose_thread_label": "Loose threads",  # THE_THREAD.md bold-label block the Loose Threads source pulls from
    "intrusive_loose_repeat_window_hours": "168",   # a surfaced loose thread is disqualified from re-firing for this long
    # Fold a newly-inferred affinity into an existing near-identical one (LLM canonicalize).
    "affinity_dedupe_enabled": "1",
    # Daily reflection: nightly pass draws general takeaways, picks a day-mood, maybe a dream.
    "reflection_enabled": "1",
    "reflection_hour": "4",              # server-local hour the nightly pass runs (APScheduler local clock)
    "reflection_dream_chance": "0.35",   # probability a dream is generated on a given night
    "reflection_lookback_hours": "24",   # window of activity a reflection considers
    "reflection_conclusions_prompt": (
        "You are the character described below, writing a few lines in a private journal at the end of "
        "the day. Given today's conversations, your emotional arc, and anything you did on your own, "
        "surface 2-4 SHORT, GENERAL reflections in your own first-person voice — impressions and "
        "takeaways ('today felt scattered', 'I keep drifting back to the outdoors'), NOT pointed "
        "factual claims or to-do items. Stay completely in character; keep each to one sentence. "
        "Return ONLY JSON: {\"conclusions\": [\"...\", ...]}."
    ),
    "reflection_dream_prompt": (
        "You are the character described below. Compose a DREAM they had tonight, woven from the day's "
        "mood and a couple of its threads — surreal, first-person, vivid but brief (3-5 sentences). "
        "Then name the single mood they wake in from the allowed list. Return ONLY JSON: "
        "{\"dream\": \"...\", \"wake_mood\": \"<one mood id>\"}."
    ),
    # The Den: feed yesterday's entries digest into the reflection; chance a dream weaves in a text entry.
    "den_enabled": "1",
    "reflection_dream_den_chance": "0.5",
    "reflection_dream_den_prompt": (
        "You are the character described below. Compose a DREAM they had tonight — surreal, "
        "first-person, vivid but brief (3-5 sentences). A MEMORY from yesterday is provided below; let "
        "it surface in the dream as distorted, symbolic imagery — do NOT recount or explain it, just "
        "let its feeling and images bleed in. Then name the single mood they wake in from the allowed "
        "list. Return ONLY JSON: {\"dream\": \"...\", \"wake_mood\": \"<one mood id>\"}."
    ),
}


async def get_low_signal_emotions() -> set:
    """Emotions filtered out of the 'top' emotions (dominant GoEmotions noise)."""
    raw = await get_setting("low_signal_emotions", DEFAULTS["low_signal_emotions"])
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


async def get_impulse_capabilities(enabled_only: bool = True) -> list:
    """The arbiter's capability manifest (see DEFAULTS['impulse_capabilities']). Parsed from JSON;
    malformed entries are skipped. enabled_only filters to capabilities cleared for use in cron."""
    import json
    raw = await get_setting("impulse_capabilities", DEFAULTS["impulse_capabilities"])
    try:
        items = json.loads(raw or "[]")
    except (ValueError, TypeError):
        items = []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("id") or not it.get("intent"):
            continue
        if enabled_only and not it.get("enabled"):
            continue
        out.append(it)
    return out


async def _load_capabilities_raw() -> list:
    """All capabilities (incl. disabled/partial), unfiltered — for the editor + CRUD."""
    import json
    raw = await get_setting("impulse_capabilities", DEFAULTS["impulse_capabilities"])
    try:
        items = json.loads(raw or "[]")
    except (ValueError, TypeError):
        items = []
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


async def _save_capabilities(items: list) -> None:
    import json
    await set_settings({"impulse_capabilities": json.dumps(items)})


async def upsert_capability(cap: dict) -> dict:
    """Insert or update one capability by id (normalized). Requires id, class, description."""
    cid = str(cap.get("id") or "").strip().lower().replace(" ", "-")
    if not cid:
        raise ValueError("capability id is required")
    cls = str(cap.get("class") or "").strip().lower()
    if cls not in ("inward", "outward"):
        raise ValueError("class must be 'inward' or 'outward'")
    if not str(cap.get("description") or "").strip():
        raise ValueError("description is required")
    # mood_tags: normalized, deduped mood ids that make this impulse more likely in those moods.
    mood_tags: list[str] = []
    for m in cap.get("mood_tags") or []:
        mid = str(m).strip().lower()
        if mid and mid not in mood_tags:
            mood_tags.append(mid)
    clean = {
        "id": cid,
        "class": cls,
        "intent": (str(cap.get("intent") or "").strip() or cid),
        "enabled": bool(cap.get("enabled")),
        "backing_kind": (str(cap.get("backing_kind") or "tool").strip() or "tool"),
        "skill": str(cap.get("skill") or "").strip(),
        "description": str(cap.get("description") or "").strip(),
        "operational_hint": str(cap.get("operational_hint") or "").strip(),
        "mood_tags": mood_tags,
    }
    items = await _load_capabilities_raw()
    for i, it in enumerate(items):
        if it.get("id") == cid:
            items[i] = clean
            break
    else:
        items.append(clean)
    await _save_capabilities(items)
    return clean


async def delete_capability(cid: str) -> None:
    await _save_capabilities([it for it in await _load_capabilities_raw() if it.get("id") != cid])


async def toggle_capability(cid: str) -> None:
    items = await _load_capabilities_raw()
    for it in items:
        if it.get("id") == cid:
            it["enabled"] = not it.get("enabled")
    await _save_capabilities(items)


async def get_mood_adjacency() -> dict:
    """Directed mood transition graph {mood_id: set(moods it may move TO)}. Directed so
    escalation (flirty→horny) and cooldown (horny→content) can differ."""
    import json
    raw = await get_setting("mood_adjacency", DEFAULTS["mood_adjacency"])
    try:
        graph = json.loads(raw or "{}")
    except (ValueError, TypeError):
        graph = {}
    return {m: set(ns or []) for m, ns in graph.items()}


async def get_transition_config() -> dict:
    """(enabled, inertia_bonus, jump_penalty, adjacency) for natural mood transitions."""
    enabled = (await get_setting("mood_transitions_enabled", "1")) == "1"
    try:
        inertia = max(0, int(float(await get_setting("mood_inertia_bonus", "2"))))
    except (TypeError, ValueError):
        inertia = 2
    try:
        penalty = max(0, int(float(await get_setting("mood_jump_penalty", "3"))))
    except (TypeError, ValueError):
        penalty = 3
    adjacency = await get_mood_adjacency()
    return {"enabled": enabled, "inertia": inertia, "penalty": penalty, "adjacency": adjacency}


async def get_mood_cascade() -> tuple[bool, dict]:
    """(enabled, {mood_id: {"to": mood_id, "at": int, "release": int}}). When a source mood's
    EARNED votes reach 'at' its votes transfer onto 'to' (escalation). Hysteresis: once escalated,
    the source only needs to stay >= 'release' (< 'at') to keep feeding the target, so escalations
    don't chatter at the boundary. 'release' defaults to a 2-vote deadband below 'at'."""
    import json
    enabled = (await get_setting("mood_cascade_enabled", "1")) == "1"
    raw = await get_setting("mood_cascade", DEFAULTS["mood_cascade"])
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        data = {}
    out: dict = {}
    for m, c in (data.items() if isinstance(data, dict) else []):
        if isinstance(c, dict) and c.get("to"):
            try:
                at = int(c.get("at", 99))
                release = int(c.get("release", max(1, at - 2)))
                out[str(m)] = {"to": str(c["to"]), "at": at, "release": max(1, min(release, at))}
            except (TypeError, ValueError):
                pass
    return enabled, out


async def get_mood_decay_config() -> dict:
    """(enabled, grace, rate, cooldown) for homeostatic mood decay, all in rounds/votes."""
    async def _i(key: str, default: int) -> int:
        try:
            return max(0, int(float(await get_setting(key, str(default)))))
        except (TypeError, ValueError):
            return default
    return {
        "enabled": (await get_setting("mood_decay_enabled", "1")) == "1",
        "grace": await _i("mood_decay_grace", 5),
        "rate": await _i("mood_decay_rate", 1),
        "cooldown": await _i("mood_decay_cooldown", 4),
    }


async def get_emotion_taxonomy() -> list:
    """The configured emotion label list the scorer rates against (order preserved, deduped)."""
    raw = await get_setting("emotion_taxonomy", DEFAULTS["emotion_taxonomy"])
    seen, out = set(), []
    for e in (raw or "").replace("\n", ",").split(","):
        e = e.strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


async def get_all_settings() -> dict:
    """Return every known setting, filling any gaps with defaults."""
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT key, value FROM app_settings")
        stored = {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await conn.close()
    return {k: stored.get(k, default) for k, default in DEFAULTS.items()}


async def get_setting(key: str, default: str | None = None) -> str | None:
    conn = await get_ego_db()
    try:
        cursor = await conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    finally:
        await conn.close()
    if row is not None:
        return row[0]
    return default if default is not None else DEFAULTS.get(key)


async def set_settings(updates: dict) -> None:
    """Upsert a batch of settings. Empty string values are written as-is so the
    panel can clear a field; callers should skip keys they don't want to touch."""
    now = time.time()
    conn = await get_ego_db()
    try:
        for key, value in updates.items():
            if key not in DEFAULTS:
                continue
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        await conn.commit()
    finally:
        await conn.close()


async def get_llm_config() -> dict:
    """Resolved LLM connection config for the client + worker."""
    s = await get_all_settings()

    def _f(key: str, fallback: float) -> float:
        try:
            return float(s.get(key))
        except (TypeError, ValueError):
            return fallback

    return {
        "backend": s.get("llm_backend") or "deepseek",
        "base_url": (s.get("llm_base_url") or "").rstrip("/"),
        "api_key": s.get("llm_api_key") or "",
        "model": s.get("llm_model") or "",
        "temperature": _f("llm_temperature", 0.7),
    }


async def get_evolution_config() -> dict:
    """Bounded-evolution tuning knobs."""
    s = await get_all_settings()

    def _f(key: str, fallback: float) -> float:
        try:
            return float(s.get(key))
        except (TypeError, ValueError):
            return fallback

    return {
        "alpha": _f("evolution_alpha", 0.2),
        "seed_band": _f("seed_deviation_band", 0.35),
        "trait_drift": _f("trait_drift_delta", 0.1),
    }
