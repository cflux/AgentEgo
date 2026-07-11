import aiosqlite
import time as _time

SCHEMA_VERSION = 17

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,
    session_id  TEXT,
    platform    TEXT,
    user_id     TEXT,
    chat_id     TEXT,
    received_at REAL    NOT NULL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session  ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_type     ON events(event_type);

CREATE TABLE IF NOT EXISTS platform_stats (
    platform         TEXT NOT NULL,
    stat_date        TEXT NOT NULL,
    session_count    INTEGER DEFAULT 0,
    agent_turn_count INTEGER DEFAULT 0,
    PRIMARY KEY (platform, stat_date)
);

CREATE TABLE IF NOT EXISTS module_data (
    module     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (module, key)
);

CREATE TABLE IF NOT EXISTS moods (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    color       TEXT NOT NULL DEFAULT '#888888',
    icon        TEXT,
    min_votes   INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_moods (
    profile_name TEXT PRIMARY KEY,
    mood_id      TEXT,
    vote_count   INTEGER,
    computed_at  REAL,
    breakdown    TEXT
);

CREATE TABLE IF NOT EXISTS mood_thresholds (
    profile_name TEXT NOT NULL,
    mood_id      TEXT NOT NULL,
    min_votes    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (profile_name, mood_id)
);

CREATE TABLE IF NOT EXISTS mood_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    prev_mood_id TEXT,
    mood_id      TEXT,
    vote_count   INTEGER,
    breakdown    TEXT,
    changed_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mood_history ON mood_history(profile_name, changed_at DESC);

CREATE TABLE IF NOT EXISTS mood_defaults (
    profile_name TEXT NOT NULL,
    mood_id      TEXT NOT NULL,
    PRIMARY KEY (profile_name, mood_id)
);

CREATE TABLE IF NOT EXISTS topic_aliases (
    raw        TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS rounds (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    round_index     INTEGER NOT NULL,
    start_ts        REAL NOT NULL,
    end_ts          REAL NOT NULL,
    msg_count       INTEGER NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rounds_profile ON rounds(profile_name, end_ts DESC);
CREATE INDEX IF NOT EXISTS idx_rounds_conv ON rounds(conversation_id);

CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    part_index   INTEGER NOT NULL DEFAULT 0,
    part_total   INTEGER NOT NULL DEFAULT 1,
    start_ts     REAL NOT NULL,
    end_ts       REAL NOT NULL,
    msg_count    INTEGER NOT NULL,
    title        TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_profile ON conversations(profile_name, end_ts DESC);

CREATE TABLE IF NOT EXISTS personality_traits (
    profile_name    TEXT PRIMARY KEY,
    source_hash     TEXT,
    traits_baseline TEXT,
    traits_current  TEXT,
    extracted_at    REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS affinities (
    id                TEXT PRIMARY KEY,
    profile_name      TEXT NOT NULL,
    entity            TEXT NOT NULL,
    category          TEXT,
    valence           REAL NOT NULL DEFAULT 0,
    intensity         REAL NOT NULL DEFAULT 0,
    confidence        REAL NOT NULL DEFAULT 0,
    baseline_valence  REAL,
    baseline_intensity REAL,
    source            TEXT NOT NULL DEFAULT 'inferred',
    rationale         TEXT,
    mention_count     INTEGER NOT NULL DEFAULT 1,
    first_seen        REAL,
    last_seen         REAL,
    updated_at        REAL,
    UNIQUE (profile_name, entity)
);
CREATE INDEX IF NOT EXISTS idx_affinity_profile ON affinities(profile_name, valence DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS impulse_log (
    id           TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    action_id    TEXT,
    label        TEXT,
    prompt       TEXT,
    mood_id      TEXT,
    idle_minutes REAL,
    fired_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impulse_log_profile ON impulse_log(profile_name, fired_at DESC);

CREATE TABLE IF NOT EXISTS reflections (
    id             TEXT PRIMARY KEY,
    profile_name   TEXT NOT NULL,
    day            TEXT NOT NULL,
    created_at     REAL NOT NULL,
    conclusions    TEXT,
    day_mood_id    TEXT,
    dream_occurred INTEGER NOT NULL DEFAULT 0,
    dream_prompt   TEXT,
    dream_mood_id  TEXT,
    dream_consumed INTEGER NOT NULL DEFAULT 0,
    debug          TEXT,
    den            TEXT,
    UNIQUE(profile_name, day)
);
CREATE INDEX IF NOT EXISTS idx_reflections_profile ON reflections(profile_name, day DESC);

CREATE TABLE IF NOT EXISTS mood_corrections (
    id             TEXT PRIMARY KEY,
    profile_name   TEXT NOT NULL,
    target_mood    TEXT NOT NULL,
    agent_emotions TEXT NOT NULL,          -- JSON {emotion: weight}
    relation       TEXT NOT NULL DEFAULT 'none',  -- none | mutual | mismatch
    user_emotions  TEXT,                   -- JSON {emotion: weight}; used when relation != none
    mode           TEXT,                   -- JSON list of conversation modes (undercurrent)
    topic_contains TEXT,                   -- JSON list of topic substrings
    strength       REAL NOT NULL DEFAULT 0.6,
    note           TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mood_corrections_profile ON mood_corrections(profile_name);

CREATE TABLE IF NOT EXISTS mood_exits (
    id             TEXT PRIMARY KEY,
    profile_name   TEXT NOT NULL,
    source_mood    TEXT,                   -- NULL = fires from any mood
    target_mood    TEXT NOT NULL,
    condition_type TEXT NOT NULL DEFAULT 'llm',   -- 'llm' | 'signal'
    condition      TEXT NOT NULL,          -- JSON: {"text": ...} for llm, {"metric","op","value"} for signal
    hard           INTEGER NOT NULL DEFAULT 0,     -- 1 = snap regardless; 0 = only unstick a stale source
    note           TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mood_exits_profile ON mood_exits(profile_name);
"""

_DEFAULT_MOODS = [
    ("focused",    "Focused",    "Concentrated, productive work sessions",             "#4a9eff", "🎯", 2),
    ("tired",      "Tired",      "Too many similar sessions without variety",           "#8a8a9a", "😴", 2),
    ("frustrated", "Frustrated", "Negative sentiment signals recently",                 "#d94f4f", "😤", 1),
    ("social",     "Social",     "Lots of social or playful interactions",              "#4caf50", "😊", 2),
    ("creative",   "Creative",   "Creative and generative work dominating",             "#ff9f4a", "✨", 2),
    ("content",    "Content",    "Positive sentiment and balanced variety",             "#4adbae", "😌", 2),
    ("curious",    "Curious",    "Informative sessions and active exploration",         "#b47aff", "🔍", 2),
]


async def _seed_moods(conn) -> None:
    row = await (await conn.execute("SELECT COUNT(*) FROM moods")).fetchone()
    if row and row[0] > 0:
        return
    now = _time.time()
    await conn.executemany(
        "INSERT OR IGNORE INTO moods (id, name, description, color, icon, min_votes, created_at) VALUES (?,?,?,?,?,?,?)",
        [(mid, name, desc, color, icon, mv, now) for mid, name, desc, color, icon, mv in _DEFAULT_MOODS],
    )


# Runtime-editable config defaults (the model control panel overrides these in app_settings).
_DEFAULT_SETTINGS = {
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
    "impulse_inward_hour_start": "6",
    "impulse_inward_hour_end": "24",
    "impulse_outward_hour_start": "6",
    "impulse_outward_hour_end": "20",
    "impulse_outward_min_idle_minutes": "30",
    "impulse_outward_backoff_cap_min": "480",
    # Phase 4 solitude/pacing (see settings_store.DEFAULTS for docs).
    "solitude_enabled": "1",
    "solitude_onset_min": "90",
    "solitude_rate_per_hour": "2.0",
    "solitude_cap": "6.0",
    "solitude_targets": '{"lonely": 1.0, "bored": 0.6, "tired": 0.2}',
    "solitude_ignored_bump": "3.0",
    "solitude_ignored_halflife_hours": "6.0",
    "impulse_act_gate_enabled": "1",
    "impulse_act_probability_default": "0.6",
    "impulse_act_probability": (
        '{"bored":0.9,"lonely":0.9,"curious":0.85,"social":0.85,"creative":0.8,"playful":0.8,"flirty":0.8,'
        '"horny":0.8,"hopeful":0.7,"frustrated":0.7,"anxious":0.6,"affectionate":0.6,"proud":0.6,'
        '"focused":0.5,"jealous":0.5,"sad":0.4,"content":0.35,"tired":0.3}'
    ),
    "impulse_action_daily_budget": '{"image-create": 6}',
    # Impulse v2 arbiter (see settings_store.DEFAULTS for the authoritative copy + docs).
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
    "scoring_backend": "llm",
    "emotion_taxonomy": (
        "admiration,amusement,anger,annoyance,approval,caring,confusion,curiosity,desire,"
        "disappointment,disapproval,disgust,embarrassment,excitement,fear,gratitude,grief,joy,"
        "love,nervousness,optimism,pride,realization,relief,remorse,sadness,surprise,neutral,"
        "arousal,lust,horny,yearning,longing,tenderness,affection,infatuation,passion,"
        "possessiveness,boredom,jealousy,contentment,trust,anticipation,awe,loneliness,contempt"
    ),
    "sentiment_llm_url": "http://localhost:11434",
    "sentiment_llm_model": "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest",
    "mood_local_llm_url": "http://host.docker.internal:11434",
    "mood_local_llm_model": "",
    "llm_mood_votes_enabled": "1",
    "llm_mood_threshold": "6",
    "llm_mood_weight": "1",
    "mood_transitions_enabled": "1",
    "mood_inertia_bonus": "2",
    "mood_jump_penalty": "3",
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
    "mood_cascade_enabled": "1",
    "mood_cascade": '{"flirty":{"to":"horny","at":12},"curious":{"to":"focused","at":10},"lonely":{"to":"sad","at":8},"frustrated":{"to":"tired","at":9},"bored":{"to":"tired","at":8},"jealous":{"to":"frustrated","at":8}}',
    "mood_decay_enabled": "1",
    "mood_decay_grace": "5",
    "mood_decay_rate": "1",
    "mood_decay_cooldown": "4",   # buffer rounds added to grace for the vacated-mood cooldown
    # Homeostasis v2 (see settings_store.DEFAULTS for docs): tenure-shaped incumbent bias + hysteresis.
    "mood_switch_margin": "1",
    "mood_fuzzy_select": "0",
    "mood_fuzzy_band": "2",
    # Mood scoring (see settings_store.DEFAULTS for docs).
    "mood_backbone_scale": "2.5",
    "mood_recency_halflife": "8",
    "mood_correction_scale": "0.5",
    "mood_correction_mutual_bonus": "0.5",
    "mood_correction_mode_baseline": "0.1",
    "mood_correction_headroom": "20",
    "mood_solo_positive_weight": "0.6",
    "mood_solo_negative_weight": "0.3",
    "mood_directive_enabled": "1",
    "mood_directive_template": (
        "## Current disposition\n"
        "You've recently been feeling **{mood}** ({description}). Let it colour your tone, but follow "
        "the user's lead and let it pass naturally — don't force it or escalate it."
    ),
    "mood_directive_file": "",
    "affinity_dedupe_enabled": "1",
    "reflection_enabled": "1",
    "reflection_hour": "4",
    "reflection_dream_chance": "0.35",
    "reflection_lookback_hours": "24",
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


async def _seed_settings(conn) -> None:
    now = _time.time()
    await conn.executemany(
        "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?,?,?)",
        [(k, v, now) for k, v in _DEFAULT_SETTINGS.items()],
    )


async def run_migrations(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_DDL)
        row = await (await conn.execute("SELECT version FROM schema_version LIMIT 1")).fetchone()
        current_version = row[0] if row else 0

        if current_version < 3:
            # conversations table created by DDL above; no ALTER needed
            pass

        if current_version < 4:
            # personality_traits, affinities, app_settings created by DDL above; no ALTER needed
            pass

        if current_version < 5:
            # impulse_actions, impulse_log created by DDL above; no ALTER needed
            pass

        if current_version < 6:
            try:
                await conn.execute("ALTER TABLE impulse_actions ADD COLUMN mood_negate INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # column already exists

        if current_version < 7:
            # mood_defaults created by DDL above; no ALTER needed
            pass

        if current_version < 8:
            # topic_aliases created by DDL above; no ALTER needed
            pass

        if current_version < 9:
            # rounds created by DDL above; no ALTER needed
            pass

        if current_version < 10:
            # mood_history created by DDL above; no ALTER needed
            pass

        if current_version < 11:
            # reflections created by DDL above; no ALTER needed
            pass

        if current_version < 12:
            try:
                await conn.execute("ALTER TABLE reflections ADD COLUMN debug TEXT")
            except Exception:
                pass  # column already exists

        if current_version < 13:
            try:
                await conn.execute("ALTER TABLE reflections ADD COLUMN den TEXT")
            except Exception:
                pass  # column already exists

        if current_version < 14:
            # mood_corrections created by DDL above; no ALTER needed
            pass

        if current_version < 15:
            # mood_exits created by DDL above; no ALTER needed
            pass

        if current_version < 16:
            # Legacy 57-rule engine removed; drop its now-inert table.
            await conn.execute("DROP TABLE IF EXISTS mood_rules")

        if current_version < 17:
            # Impulse v1 weighted-lottery removed (v2 arbiter drives); drop its now-inert table.
            await conn.execute("DROP TABLE IF EXISTS impulse_actions")

        if current_version < SCHEMA_VERSION:
            if current_version == 0:
                await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                await conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        await _seed_moods(conn)
        await _seed_settings(conn)
        await conn.commit()
