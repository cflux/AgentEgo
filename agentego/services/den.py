"""Read-only access to the Den — a per-profile, file-based journal of *meaning*
(`~/.the-den/<profile>/`). AgentEgo never writes here; it browses/searches entries for the web UI and
feeds a daily digest into the reflection system.

Layout: `entries/YYYY/MM/DD_slug.md` (YAML frontmatter + markdown body), `index.json` (tag → paths),
and a `files/` symlink to the media dir. Entries with a `file:` field reference multimedia; text-only
entries (no `file:`) are the ones eligible to seed a dream.
"""
import json
import os
from datetime import date, datetime
from pathlib import Path

import yaml

from ..config import settings

ENTRY_TYPES = ("feeling", "art", "discovery", "transcript", "gift", "fantasy")


def den_root() -> Path:
    return Path(settings.den_path)


def _profile_dir(profile: str) -> Path:
    return den_root() / profile


def list_den_profiles() -> list[str]:
    """Profiles that have a Den (a subdir with entries/ or index.json)."""
    root = den_root()
    out = []
    try:
        for p in sorted(root.iterdir()):
            if p.is_dir() and ((p / "entries").is_dir() or (p / "index.json").is_file()):
                out.append(p.name)
    except OSError:
        pass
    return out


def has_den(profile: str) -> bool:
    d = _profile_dir(profile)
    return (d / "entries").is_dir() or (d / "index.json").is_file()


def load_index(profile: str) -> dict:
    """The tag → [relative paths] registry, or {} if absent/unreadable."""
    try:
        return json.loads((_profile_dir(profile) / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse leading `---\n…\n---` YAML frontmatter; return (meta, body)."""
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            raw = parts[0][3:]  # drop leading ---
            body = parts[1].lstrip("\n")
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, body
    return {}, text


def _rel(profile: str, path: Path) -> str:
    try:
        return str(path.relative_to(_profile_dir(profile)))
    except ValueError:
        return path.name


def parse_entry(profile: str, path: Path) -> dict | None:
    try:
        text = path.read_text()
        st = path.stat()
    except OSError:
        return None
    meta, body = _split_frontmatter(text)
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        importance = float(meta.get("importance")) if meta.get("importance") is not None else None
    except (TypeError, ValueError):
        importance = None
    file_field = meta.get("file")
    return {
        "profile": profile,
        "relpath": _rel(profile, path),
        "slug": path.stem,
        "date": str(meta.get("date") or ""),
        "type": str(meta.get("type") or ""),
        "tags": [str(t) for t in tags],
        "importance": importance,
        "mood": str(meta.get("mood") or ""),
        "file": str(file_field) if file_field else None,
        "has_media": bool(file_field),
        "summary": str(meta.get("summary") or ""),
        "body": body.strip(),
        "mtime": st.st_mtime,
    }


def _entry_paths(profile: str) -> list[Path]:
    entries_dir = _profile_dir(profile) / "entries"
    if not entries_dir.is_dir():
        return []
    return [p for p in entries_dir.rglob("*.md") if p.is_file()]


def list_entries(profile: str) -> list[dict]:
    """All entries, newest first (by date then mtime)."""
    out = [e for p in _entry_paths(profile) if (e := parse_entry(profile, p))]
    out.sort(key=lambda e: (e["date"], e["mtime"]), reverse=True)
    return out


def get_entry(profile: str, relpath: str) -> dict | None:
    """Fetch one entry by its profile-relative path (traversal-safe)."""
    base = _profile_dir(profile).resolve()
    target = (base / relpath).resolve()
    if base not in target.parents and target != base:
        return None
    if not target.is_file():
        return None
    return parse_entry(profile, target)


def search_entries(profile: str, q: str = "", tag: str = "", type: str = "") -> list[dict]:
    entries = list_entries(profile)
    if tag:
        tl = tag.strip().lower()
        entries = [e for e in entries if tl in [t.lower() for t in e["tags"]]]
    if type:
        entries = [e for e in entries if e["type"] == type]
    if q:
        ql = q.strip().lower()
        entries = [e for e in entries
                   if ql in e["summary"].lower() or ql in e["body"].lower()
                   or any(ql in t.lower() for t in e["tags"]) or ql in e["slug"].lower()]
    return entries


def all_tags(profile: str) -> list[str]:
    return sorted(load_index(profile).keys())


# --- Media (validated serving) ---

def resolve_media(profile: str, req_path: str) -> Path | None:
    """Return a safe absolute Path for a media file *only* if some entry references it via its
    `file:` field. Guards against path traversal / arbitrary file reads."""
    if not req_path:
        return None
    want = os.path.realpath(req_path)
    for e in list_entries(profile):
        if e["file"] and os.path.realpath(e["file"]) == want:
            p = Path(want)
            return p if p.is_file() else None
    return None


# --- Reflection helpers (date-scoped) ---

def _as_date_str(d) -> str:
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    return str(d)


def _mtime_date(mtime: float) -> str:
    # Local calendar date (container TZ), to match how "yesterday" is computed elsewhere.
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def entries_on(profile: str, day) -> list[dict]:
    """Entries *created* on `day` (their `date:` field)."""
    ds = _as_date_str(day)
    return [e for e in list_entries(profile) if e["date"] == ds]


def top_entries(profile: str, day, n: int = 5) -> list[dict]:
    """Up to n entries created on `day`, ranked by importance (the Den's rating), highest first."""
    ranked = sorted(entries_on(profile, day), key=lambda e: (e["importance"] or 0.0), reverse=True)
    return ranked[:n]


def tags_touched(profile: str, day) -> list[str]:
    """Unique tags of entries created OR edited on `day` (date field or file mtime)."""
    ds = _as_date_str(day)
    tags: list[str] = []
    for e in list_entries(profile):
        if e["date"] == ds or _mtime_date(e["mtime"]) == ds:
            for t in e["tags"]:
                if t not in tags:
                    tags.append(t)
    return tags


def text_entries(profile: str, day) -> list[dict]:
    """Entries created on `day` that carry writing (non-empty body) — the ones eligible to seed a
    dream. Media-only entries (a file with no text) are excluded; media+text entries qualify and
    seed the dream from their text portion, same as text-only ones."""
    return [e for e in entries_on(profile, day) if e["body"]]


def reflection_digest(profile: str, day) -> dict:
    """Digest for the reflection summary: count of yesterday's entries, top-5 by importance, and the
    tags touched. Never includes bodies (per the Den brief)."""
    top = top_entries(profile, day, 5)
    return {
        "day": _as_date_str(day),
        "count": len(entries_on(profile, day)),
        "top": [{"slug": e["slug"], "type": e["type"], "importance": e["importance"],
                 "tags": e["tags"], "summary": e["summary"], "relpath": e["relpath"]} for e in top],
        "tags": tags_touched(profile, day),
    }
