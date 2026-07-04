import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import settings
from .db.migrations import run_migrations
from .services.stats import start_scheduler
from .routers import events, sessions, dashboard, sentiment, topic, mood, preferences, config_panel, impulse, soul, reflection, den
from .modules import load_modules

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

try:
    _DISPLAY_TZ = ZoneInfo(settings.display_timezone)
except Exception:
    _DISPLAY_TZ = timezone.utc


def _fmt_ts(value, fmt=None) -> str:
    """Render a Unix timestamp in the display timezone, human-friendly:
    'Today, 1:41 PM' / 'Yesterday, 9:30 AM' / 'Jun 27, 3:00 PM' / 'Jun 27, 2025, 3:00 PM'."""
    if value is None or value == "":
        return "—"
    try:
        dt = datetime.fromtimestamp(float(value), _DISPLAY_TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)[:19]
    now = datetime.now(_DISPLAY_TZ)
    t = dt.strftime("%-I:%M %p")
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"Today, {t}"
    if days == 1:
        return f"Yesterday, {t}"
    if dt.year == now.year:
        return f"{dt.strftime('%b %-d')}, {t}"
    return f"{dt.strftime('%b %-d, %Y')}, {t}"


def _den_md(text) -> "Markup":
    """Minimal, safe markdown for Den entry bodies: escape, then **bold** / *italic* / paragraphs."""
    from markupsafe import Markup, escape
    import re
    if not text:
        return Markup("")
    out = []
    for para in re.split(r"\n\s*\n", str(text).strip()):
        safe = str(escape(para))
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        safe = re.sub(r"\*(.+?)\*", r"<em>\1</em>", safe)
        out.append("<p>" + safe.replace("\n", "<br>") + "</p>")
    return Markup("".join(out))


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(settings.ego_db_path), exist_ok=True)
    await run_migrations(settings.ego_db_path)
    scheduler = start_scheduler()
    load_modules(app)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="AgentEgo", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Register the timestamp filter on all Jinja2Templates instances by patching
# the shared environment after routers are imported.
_templates_dir = Path(__file__).parent / "templates"
_shared_templates = Jinja2Templates(directory=str(_templates_dir))
_shared_templates.env.filters["ts"] = _fmt_ts
_shared_templates.env.filters["den_md"] = _den_md

# Patch the router instances to use the shared templates environment
dashboard.templates = _shared_templates
sessions.templates = _shared_templates
mood.templates = _shared_templates
preferences.templates = _shared_templates
config_panel.templates = _shared_templates
impulse.templates = _shared_templates
reflection.templates = _shared_templates
den.templates = _shared_templates

app.include_router(events.router)
app.include_router(sessions.router)
app.include_router(dashboard.router)
app.include_router(sentiment.router)
app.include_router(topic.router)
app.include_router(mood.router)
app.include_router(preferences.router)
app.include_router(config_panel.router)
app.include_router(impulse.router)
app.include_router(soul.router)
app.include_router(reflection.router)
app.include_router(den.router)
