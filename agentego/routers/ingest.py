"""Ingest endpoints for idoru agents (DESIGN §6b).

idoru owns its own message store and *pushes* transcript here rather than letting AgentEgo read its
DB. These endpoints register an idoru agent and accept pushed messages, writing them into the
ego-local source (ext_sessions/ext_messages) and running the existing conversation/round sync so the
sentiment/topic/mood pipeline scores them exactly as it does Hermes agents.
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..db.idoru_local import dbpath_for, ingest_messages, register_agent
from ..services.conversations import sync_recent_conversations

router = APIRouter(prefix="/api/ingest")


class RegisterAgent(BaseModel):
    name: str
    display_name: str | None = None
    meta: str | None = None


class IngestSession(BaseModel):
    id: str
    platform: str | None = None
    user_id: str | None = None
    title: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    message_count: int | None = None


class IngestMessage(BaseModel):
    id: str
    role: str
    content: str = ""
    timestamp: float


class IngestMessages(BaseModel):
    profile: str
    session: IngestSession
    messages: list[IngestMessage] = Field(default_factory=list)


@router.post("/agent", status_code=201)
async def register(agent: RegisterAgent):
    await register_agent(agent.name, agent.display_name, agent.meta)
    return {"status": "registered", "name": agent.name}


@router.post("/messages", status_code=202)
async def push_messages(body: IngestMessages):
    new = await ingest_messages(
        body.profile,
        body.session.model_dump(),
        [m.model_dump() for m in body.messages],
    )
    # Build/reconcile conversations + rounds for this agent right away so it scores promptly.
    await sync_recent_conversations(body.profile, db_path=dbpath_for(body.profile))
    return {"status": "accepted", "profile": body.profile, "new_messages": new}
