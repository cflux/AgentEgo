import json
import time
import logging
from ..db.ego import get_ego_db
from ..models.events import HookEvent

logger = logging.getLogger("agentego.events")


async def process_event(event: HookEvent) -> None:
    conn = await get_ego_db()
    try:
        await conn.execute(
            """
            INSERT INTO events
                (event_type, session_id, platform, user_id, chat_id, received_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_type,
                event.session_id,
                event.platform,
                event.user_id,
                event.chat_id,
                time.time(),
                json.dumps(event.model_dump()),
            ),
        )
        await conn.commit()
    finally:
        await conn.close()

    from ..modules import dispatch_event
    await dispatch_event(event)
