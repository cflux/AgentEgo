from fastapi import APIRouter
from ..models.events import HookEvent
from ..services.event_processor import process_event

router = APIRouter(prefix="/api")


@router.post("/events", status_code=202)
async def receive_event(event: HookEvent):
    await process_event(event)
    return {"status": "accepted"}
