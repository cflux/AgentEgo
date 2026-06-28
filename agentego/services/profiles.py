from pathlib import Path
from ..config import settings


def discover_profiles() -> list[dict]:
    """Return [{name, db_path}] for the default profile + any ~/.hermes/profiles/<name>/."""
    home = Path(settings.hermes_db_path).parent
    result = [{"name": "default", "db_path": settings.hermes_db_path}]
    profiles_dir = home / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.iterdir()):
            db = p / "state.db"
            if p.is_dir() and db.exists():
                result.append({"name": p.name, "db_path": str(db)})
    return result


def resolve_profile(name: str) -> str | None:
    """Return the db_path for a given profile name, or None if not found."""
    for p in discover_profiles():
        if p["name"] == name:
            return p["db_path"]
    return None
