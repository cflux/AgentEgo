import aiosqlite

SCHEMA_VERSION = 1

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
"""


async def run_migrations(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_DDL)
        row = await (await conn.execute("SELECT version FROM schema_version LIMIT 1")).fetchone()
        if row is None:
            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        await conn.commit()
