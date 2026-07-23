"""Intrusive thoughts management page (`/intrusive`).

A per-profile catalog of short prompts that piggyback on the mood directive on some turns. Each has a
base weight and a tri-state per-mood association (positive / neutral / negative). A single special
"Loose Threads" entry pulls its content live from the agent's Den instead of a fixed prompt.

CRUD + selection math live in services/intrusive_thoughts; this module is the FastAPI + HTMX shell,
modeled on the /moods CRUD in routers/mood.py.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..services import intrusive_thoughts as _it
from ..services import settings_store, den
from ..services.mood_engine import get_cached_mood
from ..services.profiles import discover_profiles
from .mood import _get_moods

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_SETTING_KEYS = ("intrusive_thoughts_enabled", "intrusive_thought_probability",
                 "intrusive_positive_multiplier", "intrusive_negative_multiplier",
                 "intrusive_thought_template", "intrusive_loose_thread_label")


def _parse_mood_assoc(form, moods) -> dict:
    """Read the tri-state mood picker: a select named `assoc_<mood_id>` per mood (positive/neutral/
    negative). Neutral (and anything else) is dropped."""
    out = {}
    for m in moods:
        pol = form.get(f"assoc_{m['id']}")
        if pol in ("positive", "negative"):
            out[m["id"]] = pol
    return out


async def _selection_ctx(profile: str) -> dict:
    """Current mood + global gate/multipliers, for computing/rendering effective weights + probabilities."""
    mood = await get_cached_mood(profile)
    pos_mult, neg_mult = await _it._multipliers()
    try:
        gate = float(await settings_store.get_setting("intrusive_thought_probability", "0.15"))
    except (TypeError, ValueError):
        gate = 0.15
    return {"mood": mood, "mood_id": (mood or {}).get("id"),
            "pos_mult": pos_mult, "neg_mult": neg_mult, "gate_prob": max(0.0, min(1.0, gate))}


def _totals(enabled: list, moods: list, ctx: dict) -> dict:
    """Sum of effective weight across all enabled thoughts, per mood (+ the current mood) — the
    denominator for a thought's selection share."""
    pos, neg = ctx["pos_mult"], ctx["neg_mult"]
    totals = {m["id"]: sum(_it.effective_weight(t, m["id"], pos, neg) for t in enabled) for m in moods}
    totals["__cur__"] = sum(_it.effective_weight(t, ctx["mood_id"], pos, neg) for t in enabled)
    return totals


def _decorate_one(t: dict, totals: dict, ctx: dict, moods: list, by_id: dict) -> dict:
    """Attach effective weight, current-mood fire probability, and the per-mood probability breakdown.

    A thought's per-turn fire probability under a mood = gate × share, where share is its effective
    weight over the total effective weight of all enabled thoughts under that mood. Disabled thoughts
    never fire (share 0) and are excluded from the totals."""
    pos, neg, gate = ctx["pos_mult"], ctx["neg_mult"], ctx["gate_prob"]
    en = t.get("enabled")
    eff_cur = _it.effective_weight(t, ctx["mood_id"], pos, neg)
    t["eff"] = round(eff_cur, 3)
    share_cur = (eff_cur / totals["__cur__"]) if (en and totals["__cur__"] > 0) else 0.0
    t["share_cur"] = share_cur
    t["final_cur"] = gate * share_cur
    t["mood_probs"] = []
    for m in moods:
        eff = _it.effective_weight(t, m["id"], pos, neg)
        tot = totals.get(m["id"], 0.0)
        share = (eff / tot) if (en and tot > 0) else 0.0
        t["mood_probs"].append({
            "id": m["id"], "name": m["name"], "icon": m.get("icon", ""),
            "polarity": (t.get("mood_assoc") or {}).get(m["id"]), "final": gate * share,
        })
    t["assoc_moods"] = [
        {**by_id.get(mid, {"id": mid, "name": mid, "icon": ""}), "polarity": pol}
        for mid, pol in (t.get("mood_assoc") or {}).items() if mid in by_id
    ]
    return t


def _decorate_all(thoughts: list, ctx: dict, moods: list) -> list:
    by_id = {m["id"]: m for m in moods}
    totals = _totals([t for t in thoughts if t.get("enabled")], moods, ctx)
    for t in thoughts:
        _decorate_one(t, totals, ctx, moods, by_id)
    return thoughts


async def _row_ctx(request: Request, thought: dict, profile: str) -> dict:
    """Context for a single display-row render. Denominators need the whole enabled catalog, so we
    fetch it (fresh from the DB, post-commit) to compute shares — not just this one thought."""
    moods = await _get_moods()
    ctx = await _selection_ctx(profile)
    by_id = {m["id"]: m for m in moods}
    enabled = [t for t in await _it.list_thoughts(profile) if t.get("enabled")]
    _decorate_one(thought, _totals(enabled, moods, ctx), ctx, moods, by_id)
    return {"request": request, "t": thought, "profile": profile, **ctx, "moods": moods}


# --- Page ---

@router.get("/intrusive")
async def intrusive_page(request: Request, profile: str = "default"):
    moods = await _get_moods()
    ctx = await _selection_ctx(profile)
    thoughts = _decorate_all(await _it.list_thoughts(profile), ctx, moods)
    settings = await settings_store.get_all_settings()
    loose = den.get_loose_threads(profile, label=settings.get("intrusive_loose_thread_label", "Loose threads"))
    return templates.TemplateResponse("intrusive.html", {
        "request": request,
        "profiles": discover_profiles(),
        "active_profile": profile,
        "moods": moods,
        "thoughts": thoughts,
        "settings": settings,
        "profile_enabled": await _it.is_profile_enabled(profile),
        "has_loose_row": await _it.has_loose_threads_row(profile),
        "loose_threads": loose,
        **ctx,
    })


# --- CRUD ---

@router.post("/api/intrusive")
async def create_thought(request: Request, profile: str = Form("default"), kind: str = Form("static"),
                         text: str = Form(""), weight: float = Form(1.0)):
    form = await request.form()
    moods = await _get_moods()
    # Enforce a single Loose Threads source per profile.
    if kind == "loose_threads" and await _it.has_loose_threads_row(profile):
        return RedirectResponse(f"/intrusive?profile={profile}", status_code=303)
    await _it.create_thought(profile, kind=kind, text=text, weight=weight,
                             mood_assoc=_parse_mood_assoc(form, moods))
    return RedirectResponse(f"/intrusive?profile={profile}", status_code=303)


@router.get("/api/intrusive/{tid}/edit-form")
async def edit_form(request: Request, tid: str):
    thought = await _it.get_thought(tid)
    if not thought:
        return Response(status_code=404)
    return templates.TemplateResponse("partials/intrusive_edit_row.html",
                                      await _row_ctx(request, thought, thought["profile_name"]))


@router.post("/api/intrusive/{tid}/edit")
async def edit_thought(request: Request, tid: str, text: str = Form(""), weight: float = Form(1.0)):
    thought = await _it.get_thought(tid)
    if not thought:
        return Response(status_code=404)
    form = await request.form()
    moods = await _get_moods()
    fields = {"weight": weight, "mood_assoc": _parse_mood_assoc(form, moods)}
    if thought["kind"] == "static":
        fields["text"] = text
    await _it.update_thought(tid, **fields)
    fresh = await _it.get_thought(tid)
    return templates.TemplateResponse("partials/intrusive_row.html",
                                      await _row_ctx(request, fresh, fresh["profile_name"]))


@router.get("/api/intrusive/{tid}/row")
async def thought_row(request: Request, tid: str):
    thought = await _it.get_thought(tid)
    if not thought:
        return Response(status_code=404)
    return templates.TemplateResponse("partials/intrusive_row.html",
                                      await _row_ctx(request, thought, thought["profile_name"]))


@router.post("/api/intrusive/{tid}/toggle")
async def toggle_thought(request: Request, tid: str):
    thought = await _it.get_thought(tid)
    if not thought:
        return Response(status_code=404)
    await _it.toggle_thought(tid)
    fresh = await _it.get_thought(tid)
    return templates.TemplateResponse("partials/intrusive_row.html",
                                      await _row_ctx(request, fresh, fresh["profile_name"]))


@router.delete("/api/intrusive/{tid}")
async def delete_thought(tid: str):
    await _it.delete_thought(tid)
    return Response(status_code=200)


# --- Settings + per-profile enable ---

@router.post("/api/intrusive/settings")
async def update_settings(request: Request, profile: str = Form("default")):
    form = await request.form()
    updates = {"intrusive_thoughts_enabled": "1" if form.get("intrusive_thoughts_enabled") else "0"}
    for k in _SETTING_KEYS:
        if k == "intrusive_thoughts_enabled":
            continue
        v = form.get(k)
        if v is not None:
            updates[k] = v
    await settings_store.set_settings(updates)
    return RedirectResponse(f"/intrusive?profile={profile}", status_code=303)


@router.post("/api/intrusive/profile-enabled")
async def set_profile_enabled(request: Request, profile: str = Form("default")):
    form = await request.form()
    await _it.set_profile_enabled(profile, bool(form.get("enabled")))
    return RedirectResponse(f"/intrusive?profile={profile}", status_code=303)
