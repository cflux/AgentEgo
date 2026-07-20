"""The Den browser — read-only UI to browse/search an agent's file-based journal, plus a validated
media route so an entry's referenced image renders inline."""
import mimetypes
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..services import den

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


_VIEWS = ("list", "gallery")


def _list_ctx(profile: str, q: str, tag: str, type: str, sort: str, view: str) -> dict:
    sort = sort if sort in den._SORTS else "new"
    view = view if view in _VIEWS else "list"
    entries = den.search_entries(profile, q=q, tag=tag, type=type, sort=sort)
    grouped = sort in den._DATE_SORTS
    return {"entries": entries, "groups": den.group_by_month(entries) if grouped else None,
            "grouped": grouped, "active_profile": profile, "q": q, "tag": tag, "type": type,
            "sort": sort, "view": view, "types": den.ENTRY_TYPES, "sorts": den._SORTS}


@router.get("/den")
async def den_page(request: Request, profile: str = "", q: str = "", tag: str = "", type: str = "",
                   sort: str = "new", view: str = "list"):
    profiles = den.list_den_profiles()
    if not profile:
        profile = profiles[0] if profiles else ""
    ctx = _list_ctx(profile, q, tag, type, sort, view) if profile else {
        "entries": [], "groups": None, "grouped": False, "active_profile": "", "q": q, "tag": tag,
        "type": type, "sort": "new", "view": "list", "types": den.ENTRY_TYPES, "sorts": den._SORTS}
    ctx.update({"request": request, "profiles": profiles, "all_tags": den.all_tags(profile) if profile else [],
                "research_trees": den.list_research_trees(profile) if profile else [],
                "thread": den.get_thread(profile) if profile else None,
                "wikilinks": den.wikilink_targets(profile) if profile else {}})
    return templates.TemplateResponse("den.html", ctx)


@router.get("/partials/den-list")
async def den_list_partial(request: Request, profile: str, q: str = "", tag: str = "", type: str = "",
                           sort: str = "new", view: str = "list"):
    ctx = _list_ctx(profile, q, tag, type, sort, view)
    ctx["request"] = request
    return templates.TemplateResponse("partials/den_list.html", ctx)


@router.get("/den/entry")
async def den_entry(request: Request, profile: str, path: str):
    entry = den.get_entry(profile, path)
    if not entry:
        return HTMLResponse("<p style='color:#e57373;'>Entry not found.</p>", status_code=404)
    return templates.TemplateResponse(
        "partials/den_entry.html", {"request": request, "entry": entry, "active_profile": profile,
                                    "wikilinks": den.wikilink_targets(profile)})


@router.get("/den/media")
async def den_media(profile: str, path: str):
    """Serve a media file only if an entry references it (path-traversal safe)."""
    resolved = den.resolve_media(profile, path)
    if not resolved:
        return HTMLResponse("Not found", status_code=404)
    mime = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return FileResponse(str(resolved), media_type=mime)
