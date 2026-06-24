from pydantic import BaseModel
from typing import Optional


class HookEvent(BaseModel):
    event_type: str
    session_id: Optional[str] = None
    platform: str = ""
    user_id: str = ""
    chat_id: str = ""
    timestamp: float = 0.0

    model_config = {"extra": "allow"}
