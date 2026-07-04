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


def _list_ctx(profile: str, q: str, tag: str, type: str) -> dict:
    entries = den.search_entries(profile, q=q, tag=tag, type=type)
    return {"entries": entries, "active_profile": profile, "q": q, "tag": tag, "type": type,
            "types": den.ENTRY_TYPES}


@router.get("/den")
async def den_page(request: Request, profile: str = "", q: str = "", tag: str = "", type: str = ""):
    profiles = den.list_den_profiles()
    if not profile:
        profile = profiles[0] if profiles else ""
    ctx = _list_ctx(profile, q, tag, type) if profile else {
        "entries": [], "active_profile": "", "q": q, "tag": tag, "type": type, "types": den.ENTRY_TYPES}
    ctx.update({"request": request, "profiles": profiles, "all_tags": den.all_tags(profile) if profile else []})
    return templates.TemplateResponse("den.html", ctx)


@router.get("/partials/den-list")
async def den_list_partial(request: Request, profile: str, q: str = "", tag: str = "", type: str = ""):
    ctx = _list_ctx(profile, q, tag, type)
    ctx["request"] = request
    return templates.TemplateResponse("partials/den_list.html", ctx)


@router.get("/den/entry")
async def den_entry(request: Request, profile: str, path: str):
    entry = den.get_entry(profile, path)
    if not entry:
        return HTMLResponse("<p style='color:#e57373;'>Entry not found.</p>", status_code=404)
    return templates.TemplateResponse(
        "partials/den_entry.html", {"request": request, "entry": entry, "active_profile": profile})


@router.get("/den/media")
async def den_media(profile: str, path: str):
    """Serve a media file only if an entry references it (path-traversal safe)."""
    resolved = den.resolve_media(profile, path)
    if not resolved:
        return HTMLResponse("Not found", status_code=404)
    mime = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return FileResponse(str(resolved), media_type=mime)
