import time
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("agentego.stats")


async def aggregate_platform_stats() -> None:
    from ..db.ego import get_ego_db
    from ..config import settings

    cutoff = time.time() - (settings.retention_days * 86400)
    conn = await get_ego_db()
    try:
        cursor = await conn.execute(
            """
            SELECT
                platform,
                date(received_at, 'unixepoch') AS d,
                COUNT(DISTINCT session_id)      AS sc,
                SUM(CASE WHEN event_type = 'agent:start' THEN 1 ELSE 0 END) AS ac
            FROM events
            WHERE received_at >= ?
              AND event_type IN ('agent:start', 'session:start')
              AND platform != ''
            GROUP BY platform, d
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            await conn.execute(
                """
                INSERT INTO platform_stats (platform, stat_date, session_count, agent_turn_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, stat_date) DO UPDATE SET
                    session_count    = excluded.session_count,
                    agent_turn_count = excluded.agent_turn_count
                """,
                (row[0], row[1], row[2], row[3]),
            )
        await conn.commit()
        logger.debug("Stats aggregation complete: %d platform-day rows", len(rows))
    finally:
        await conn.close()


def start_scheduler() -> AsyncIOScheduler:
    from .mood_engine import refresh_all_moods

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        aggregate_platform_stats,
        "interval",
        minutes=15,
        id="platform_stats",
        max_instances=1,
    )
    # Recompute moods on a schedule so the agent-facing endpoints are pure cached reads
    # (mood computed independent of the fetch). Runs shortly after startup, then every 90s.
    scheduler.add_job(
        refresh_all_moods,
        "interval",
        seconds=90,
        id="refresh_moods",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    return scheduler
