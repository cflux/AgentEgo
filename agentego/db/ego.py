import aiosqlite
from ..config import settings


async def get_ego_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(settings.ego_db_path)
    conn.row_factory = aiosqlite.Row
    return conn
