import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import settings
from .db.migrations import run_migrations
from .services.stats import start_scheduler
from .routers import events, sessions, dashboard
from .modules import load_modules

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))


def _fmt_ts(value, fmt="%Y-%m-%d %H:%M:%S") -> str:
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(value)).strftime(fmt)
    except Exception:
        return str(value)[:19]


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

# Patch the router instances to use the shared templates environment
dashboard.templates = _shared_templates
sessions.templates = _shared_templates

app.include_router(events.router)
app.include_router(sessions.router)
app.include_router(dashboard.router)
