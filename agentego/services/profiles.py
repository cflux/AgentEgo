import sqlite3
import time
from pathlib import Path
from ..config import settings
from ..db.idoru_local import dbpath_for

# discover_profiles() is on a hot path (per-request + the 90s scheduler), so the idoru agent list is
# cached briefly: idoru agents change only on registration, so a short TTL avoids a synchronous
# SQLite read (which would block the event loop) on every call. A newly-registered agent appears
# within TTL seconds; ingest scores it immediately regardless (it syncs directly, not via discovery).
_AGENTS_TTL = 5.0
_agents_cache: dict = {"ts": -1e9, "agents": []}


def _idoru_agents() -> list[dict]:
    """Registered idoru agents (source='idoru'), cached for _AGENTS_TTL seconds. Read synchronously
    (short busy timeout) so discover_profiles() stays a plain function its many sync callers can use
    unchanged. Best-effort: any error yields the last good value (or [])."""
    now = time.monotonic()
    if now - _agents_cache["ts"] < _AGENTS_TTL:
        return _agents_cache["agents"]
    try:
        conn = sqlite3.connect(settings.ego_db_path, timeout=0.5)
        try:
            rows = conn.execute("SELECT name FROM agents WHERE source = 'idoru'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return _agents_cache["agents"]  # keep serving the last known list on a transient error
    agents = [{"name": r[0], "db_path": dbpath_for(r[0]), "source": "idoru"} for r in rows]
    _agents_cache.update(ts=now, agents=agents)
    return agents


def discover_profiles() -> list[dict]:
    """Return [{name, db_path, source}] for the default profile + any ~/.hermes/profiles/<name>/,
    plus any registered idoru agents (fed by push, not a Hermes state.db)."""
    home = Path(settings.hermes_db_path).parent
    result = [{"name": "default", "db_path": settings.hermes_db_path, "source": "hermes"}]
    profiles_dir = home / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.iterdir()):
            db = p / "state.db"
            if p.is_dir() and db.exists():
                result.append({"name": p.name, "db_path": str(db), "source": "hermes"})
    result.extend(_idoru_agents())
    return result


def resolve_profile(name: str) -> str | None:
    """Return the db_path for a given profile name, or None if not found."""
    for p in discover_profiles():
        if p["name"] == name:
            return p["db_path"]
    return None
