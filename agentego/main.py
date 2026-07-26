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
from .routers import events, sessions, dashboard, sentiment, topic, mood, preferences, config_panel, impulse, soul, reflection, den, intrusive, ingest
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


def _den_md(text, links=None, profile="") -> "Markup":
    """Safe markdown for Den entry bodies (escape-first, so no raw HTML/XSS). Handles headings, ordered/
    unordered lists with nesting, links (http/https/relative/mailto only), inline code, bold/italic,
    horizontal rules, and [[wikilinks]] — enough to render the research trees' structured notes legibly.
    `links` (from den.wikilink_targets) resolves [[wikilinks]] to clickable tree jumps; unmatched ones
    stay inert."""
    from markupsafe import Markup, escape
    from urllib.parse import quote
    import re
    if not text:
        return Markup("")

    def _js(s: str) -> str:  # safe inside a double-quoted onclick attribute
        return str(escape(s)).replace("\\", "\\\\").replace("'", "\\'")

    def _wikilink(m):
        label = m.group(1)  # already HTML-escaped (inline() escapes before this runs)
        plain = re.sub(r"<[^>]+>", "", label)  # de-entity-safe key source
        tgt = (links or {}).get(re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-"))
        if tgt:
            return (f"<a href=\"#\" onclick=\"denOpen('{_js(profile)}','{_js(tgt['relpath'])}');return false;\" "
                    f'style="color:#b47aff; font-weight:600;" title="{escape(tgt["title"])}">{label}</a>')
        # No tree yet — fall back to a Den search so the link still goes somewhere.
        href = f"/den?profile={quote(profile)}&amp;q={quote(plain)}"
        return (f'<a href="{href}" style="color:#b47aff; font-weight:500;" '
                f'title="search the Den for “{escape(plain)}”">{label}</a>')

    def inline(s: str) -> str:
        s = str(escape(s))  # escape first — everything below only re-introduces a safe subset
        s = re.sub(r"\[\[([^\]]+)\]\]", _wikilink, s)

        def _link(m):
            txt, url = m.group(1), m.group(2)
            return (f'<a href="{url}" target="_blank" rel="noopener">{txt}</a>'
                    if re.match(r"(?:https?:|mailto:|/|#)", url, re.I) else m.group(0))
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)", r"<em>\1</em>", s)
        return s

    html: list[str] = []
    stack: list[str] = []   # open list tags by nesting depth
    para: list[str] = []

    def flush_para():
        if para:
            html.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para.clear()

    def close_lists(to: int = 0):
        while len(stack) > to:
            html.append(f"</{stack.pop()}>")

    for raw in str(text).strip().split("\n"):
        stripped = raw.strip()
        h = re.match(r"(#{1,6})\s+(.*)", stripped)
        li = re.match(r"^(\s*)(?:[-*]|\d+\.)\s+(.*)", raw)
        if not stripped:
            flush_para(); close_lists(); continue
        if re.match(r"^-{3,}$|^\*{3,}$", stripped):
            flush_para(); close_lists(); html.append("<hr>"); continue
        if h:
            flush_para(); close_lists()
            level = min(6, len(h.group(1)) + 2)  # '#' -> h3 (kept small inside the modal)
            html.append(f"<h{level} style='margin:0.7em 0 0.3em; font-size:{max(0.8, 1.15-0.07*level):.2f}rem;'>"
                        f"{inline(h.group(2))}</h{level}>")
            continue
        if li:
            flush_para()
            ordered = raw.lstrip()[0].isdigit()
            tag = "ol" if ordered else "ul"
            depth = len(li.group(1)) // 2 + 1
            close_lists(depth)
            while len(stack) < depth:
                html.append(f"<{tag} style='margin:0.2em 0 0.4em 1.1rem;'>"); stack.append(tag)
            html.append("<li>" + inline(li.group(2)) + "</li>")
            continue
        para.append(stripped)

    flush_para(); close_lists()
    return Markup("".join(html))


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
intrusive.templates = _shared_templates

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
app.include_router(intrusive.router)
app.include_router(ingest.router)
