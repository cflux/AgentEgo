import sqlite3
from pathlib import Path
from ..config import settings
from ..db.idoru_local import dbpath_for


def _idoru_agents() -> list[dict]:
    """Registered idoru agents (source='idoru'), read synchronously so discover_profiles() stays a
    plain function its many sync callers can use unchanged. Best-effort: any error yields []."""
    try:
        conn = sqlite3.connect(settings.ego_db_path, timeout=1.0)
        try:
            rows = conn.execute("SELECT name FROM agents WHERE source = 'idoru'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [{"name": r[0], "db_path": dbpath_for(r[0]), "source": "idoru"} for r in rows]


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
