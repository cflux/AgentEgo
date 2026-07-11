#!/usr/bin/env python3
"""Inject a state save prompt into Tala's latest session."""
import sqlite3, time, sys, os
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from platform_config import source_clause

DB = '/home/cflux/.hermes/profiles/tala/state.db'
MSG = sys.argv[1] if len(sys.argv) > 1 else "🔄 State save — update your current-status entry."

db = sqlite3.connect(DB)
sid = db.execute(f"""
    SELECT id FROM sessions WHERE id NOT LIKE 'cron_%' AND {source_clause()}
    ORDER BY started_at DESC LIMIT 1
""").fetchone()[0]

db.execute(
    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
    (sid, MSG, time.time() + 1)
)
db.commit()
print(f"Injected into session {sid[:30]}...")
