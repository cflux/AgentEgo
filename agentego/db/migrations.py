import aiosqlite
import time as _time

SCHEMA_VERSION = 10

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

CREATE TABLE IF NOT EXISTS mood_rules (
    id           TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    mood_id      TEXT NOT NULL,
    rule_type    TEXT NOT NULL,
    params       TEXT NOT NULL,
    label        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL
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

CREATE TABLE IF NOT EXISTS impulse_actions (
    id                     TEXT PRIMARY KEY,
    profile_name           TEXT NOT NULL,
    label                  TEXT NOT NULL,
    prompt                 TEXT NOT NULL,
    required_moods         TEXT,
    min_idle_minutes       INTEGER NOT NULL DEFAULT 0,
    base_weight            REAL NOT NULL DEFAULT 1.0,
    recency_window_minutes INTEGER NOT NULL DEFAULT 240,
    enabled                INTEGER NOT NULL DEFAULT 1,
    last_fired_at          REAL,
    created_at             REAL NOT NULL,
    mood_negate            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_impulse_profile ON impulse_actions(profile_name);

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
    "mood_decay_rate": "3",
    "mood_decay_cooldown": "4",
    "mood_directive_enabled": "1",
    "mood_directive_template": (
        "## Current disposition\n"
        "You've recently been feeling **{mood}** ({description}). Let it colour your tone, but follow "
        "the user's lead and let it pass naturally — don't force it or escalate it."
    ),
    "mood_directive_file": "",
    "affinity_dedupe_enabled": "1",
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

        if current_version < 2:
            try:
                await conn.execute("ALTER TABLE mood_rules ADD COLUMN mood_gate TEXT")
            except Exception:
                pass  # column already exists

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

        if current_version < SCHEMA_VERSION:
            if current_version == 0:
                await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                await conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        await _seed_moods(conn)
        await _seed_settings(conn)
        await conn.commit()
