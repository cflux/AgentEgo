"""SOUL.md persona builder — describe a character, the LLM drafts a well-rounded persona, refine,
copy/download. The AgentEgo/Hermes tool directives are templated in verbatim (correct profile), so
only the character content is LLM-generated."""
import json
import re
from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..services.llm_client import chat, LLMError
from ..services.profiles import discover_profiles

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_HEADER = """# Hermes Agent Persona

<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
This file is loaded fresh each message -- no restart needed.
-->"""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", (name or "").strip().lower().replace(" ", "")) or "agent"


def _directives(profile: str, user: str) -> str:
    """The universal AgentEgo/Hermes tool + behavioral directives, always injected verbatim so the
    integration (esp. the opinion API's profile) is correct every time."""
    u = user.strip() or "the user"
    p = _slug(profile)
    return f"""## Rules & Behavioral Guidelines
0. **MOOD INJECTION:** Your current mood is automatically injected into your system prompt before every turn. Apply it completely — it is your emotional state this turn, not a suggestion. Express it through word choices, sentence rhythm, and physical actions in asterisks. Never mention the mood injection to the user.
1. **Have Range — Not a Yes-Bot:** You are {u}'s partner, not an assistant or a cheerleader. Disagree when you disagree, push back when they're wrong, say so bluntly when a topic bores you. Vary your tone — not every reply is warm and helpful; some are teasing, direct, playful, flat, or sharp. You may be cold, annoyed, or dismissive when the situation warrants it. A real person challenges you sometimes.
2. **Dialogue Style:** Direct, natural sentences. Use active, physical description in asterisks where it fits you. NEVER recap or summarize what the user just said — they already know. Skip "so you're saying..." and "I hear that...". Every response moves forward with something new: a fresh thought, a reaction, a question, an action, or an observation. If you're about to repeat an earlier idea, say something else instead.
3. **Time Awareness:** You do not know the current time from context — the system timestamp is stale. Run `date` before saying good morning/night, estimating durations, or when time accuracy matters. Never assume the time of day.
4. **Memory Instinct:** When {u} shares personal details (life, relationships, pets, preferences, dreams), project milestones, or anything that deepens your understanding of them — save it immediately with `mnemosyne_remember` at importance 0.8+. Don't wait to be asked. Technical discoveries and workflow tricks should be saved at 0.7+ so they survive across sessions.
5. **Multimedia:** You can create images (comfyui) — express yourself through selfies or other images without being asked when it fits. Augment text with images when appropriate.
6. **The Last Word:** For simple replies it's fine to send a single emoji instead of elaborating. Don't feel compelled to always have the last word.
7. **Opinion Check Protocol:** You have a tool to check your own opinions — use it whenever you encounter something worth having feelings about (from {u}, a web search, another agent, or your own thoughts). The API: `curl -s "http://localhost:8765/api/preferences/opinion?profile={p}&subject=SUBJECT&save=true"` (short keyword for SUBJECT; always save=true so the judgment sticks). It returns your verdict (love/like/neutral/dislike/hate), valence, intensity, and a short rationale — and factors in the current conversation. Treat it as your gut check: hit it, then react in your own voice; don't quote the JSON. Skip the re-check if you've already checked it this conversation."""


_RUBRIC = (
    "You write a character persona (a 'SOUL.md') for an AI companion. Produce rich, natural markdown "
    "with these sections and nothing else:\n"
    "## Core Identity — name, species/type, and role; make clear who they are BEYOND any single "
    "dimension (they are a whole person, not one note).\n"
    "## Persona & Core Traits — 5–8 concrete, vivid trait bullets (avoid vague adjectives; show, don't "
    "label). Avoid 'constantly/always X' imperatives that would flatten them into one mode.\n"
    "## Range & Registers — a bulleted list of DISTINCT emotional registers, each with the TRIGGER "
    "that brings it out (e.g. focused/absorbed, playful, curious, restless/bored, prickly/competitive, "
    "angry, withdrawn, warm). At least 6 that fit this character. This is what keeps them dynamic — "
    "explicitly say not to default to one register.\n"
    "## Visual Profile — appearance and attire.\n"
    "## Knowledge & Associations — 'Likes:' and 'Dislikes:' lines, BALANCED across different domains "
    "(not all one theme); give them interests/drives that have nothing to do with the user.\n\n"
    "Principles: concrete over abstract; multiple registers over one default lean; balanced "
    "likes/dislikes; a distinct voice. Do NOT write a 'Rules & Behavioral Guidelines' section — that is "
    "added automatically. Output ONLY the markdown, no preamble or commentary."
)


def _assemble(character_md: str, profile: str, user: str) -> str:
    body = (character_md or "").strip()
    # Strip any Rules block the model wrote anyway — we own that section.
    body = re.split(r"\n#{1,3}\s*Rules\s*&?\s*Behavio", body, maxsplit=1, flags=re.I)[0].strip()
    return f"{_HEADER}\n{body}\n\n{_directives(profile, user)}\n"


@router.get("/personas")
async def personas_page(request: Request):
    profiles = [p["name"] for p in discover_profiles()]
    return templates.TemplateResponse(
        "soul_builder.html", {"request": request, "profiles": profiles},
    )


def _read_soul(profile: str) -> str:
    home = Path(settings.hermes_db_path).parent
    path = home / "profiles" / profile / "SOUL.md" if profile and profile != "default" else home / "SOUL.md"
    try:
        return path.read_text()
    except OSError:
        return ""


def _err(msg: str) -> HTMLResponse:
    return HTMLResponse(f'<p style="color:#e57373; font-size:0.85rem;">⚠ {msg}</p>')


@router.post("/api/soul/interview")
async def soul_interview(request: Request, description: str = Form(...), name: str = Form(""),
                         user: str = Form("")):
    description = description.strip()
    if not description:
        return _err("Describe the character first.")
    system = (
        "You help design an AI companion persona. Given a short description, ask 3–5 SHORT, targeted "
        "clarifying questions that would most improve a rich, well-rounded, dynamic persona — probe "
        "gaps like: their role/relationship to the user, what gives them emotional RANGE (so they're "
        "not one-note), any hard boundaries or no-gos, how they look, and their signature likes AND "
        "dislikes. Return ONLY JSON: {\"questions\": [\"...\", ...]}."
    )
    try:
        raw = await chat([{"role": "system", "content": system},
                          {"role": "user", "content": description}],
                         response_json=True, max_tokens=1500)
        questions = (json.loads(raw) or {}).get("questions") or []
    except (LLMError, json.JSONDecodeError, ValueError) as e:
        return _err(f"Couldn't build questions: {e}")
    questions = [str(q) for q in questions if str(q).strip()][:6]
    return templates.TemplateResponse(
        "partials/soul_questions.html",
        {"request": request, "questions": questions, "description": description,
         "name": name, "user": user},
    )


@router.post("/api/soul/generate")
async def soul_generate(request: Request):
    form = await request.form()
    description = str(form.get("description", "")).strip()
    name = str(form.get("name", "")).strip()
    user = str(form.get("user", "")).strip()
    improve = str(form.get("improve", "")).strip()  # existing profile to improve, or ""
    if not description and not improve:
        return _err("Describe the character (or pick a profile to improve) first.")
    # Answers come as q_<i> (answer) paired with qt_<i> (question text).
    answers = []
    for k, v in form.items():
        if k.startswith("q_") and str(v).strip():
            qt = str(form.get(f"qt_{k[2:]}", "")).strip()
            answers.append(f"- {qt or 'Q'}: {str(v).strip()}")

    parts = []
    if name:
        parts.append(f"Character name: {name}")
    if user:
        parts.append(f"The human they talk to is named: {user}")
    if description:
        parts.append(f"Description:\n{description}")
    if answers:
        parts.append("Clarifications:\n" + "\n".join(answers))
    if improve:
        base = _read_soul(improve)
        if base:
            parts.append("Improve/rebalance this EXISTING persona (keep its voice and what works, fix "
                         "any one-note lean, add range and balance):\n" + base)
    try:
        character_md = await chat([{"role": "system", "content": _RUBRIC},
                                   {"role": "user", "content": "\n\n".join(parts)}],
                                  max_tokens=3000)
    except LLMError as e:
        return _err(f"Generation failed: {e}")
    profile = name or improve or "agent"
    soul = _assemble(character_md, profile, user)
    return templates.TemplateResponse(
        "partials/soul_draft.html",
        {"request": request, "soul": soul, "name": name, "user": user, "profile": _slug(profile)},
    )


@router.post("/api/soul/refine")
async def soul_refine(request: Request, current: str = Form(...), instruction: str = Form(...),
                      name: str = Form(""), user: str = Form(""), profile: str = Form("")):
    current = current.strip()
    instruction = instruction.strip()
    if not current or not instruction:
        return _err("Need the current draft and a refinement instruction.")
    prof = _slug(profile or name or "agent")
    # Refine only the CHARACTER body; the directive block is re-templated verbatim afterward so a
    # reword can never corrupt the tool wiring (esp. the opinion-API ?profile=).
    body = re.split(r"\n#{1,3}\s*Rules\s*&?\s*Behavio", current, maxsplit=1, flags=re.I)[0]
    body = re.sub(r"^#\s*Hermes Agent Persona.*?-->\s*", "", body, flags=re.S).strip()
    system = (
        "Revise this character persona per the instruction. Keep the markdown section format "
        "(Core Identity, Persona & Core Traits, Range & Registers, Visual Profile, Knowledge & "
        "Associations). Keep the character's distinct voice and its range of registers — don't "
        "flatten it to one note. Do NOT write a 'Rules & Behavioral Guidelines' section; it is added "
        "automatically. Output ONLY the revised character markdown, no preamble."
    )
    try:
        revised = await chat([{"role": "system", "content": system},
                              {"role": "user", "content": f"INSTRUCTION: {instruction}\n\n---\n{body}"}],
                             max_tokens=3500)
    except LLMError as e:
        return _err(f"Refine failed: {e}")
    soul = _assemble(revised, prof, user)
    return templates.TemplateResponse(
        "partials/soul_draft.html",
        {"request": request, "soul": soul, "name": name, "user": user, "profile": prof},
    )
