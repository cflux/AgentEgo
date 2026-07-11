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


async def _run_reflections_job() -> None:
    """Scheduler wrapper: gated by reflection_enabled inside run_reflections()."""
    from .reflection_engine import run_reflections
    try:
        await run_reflections()
    except Exception:
        logger.exception("reflection pass failed")


def _reflection_hour() -> int:
    """Best-effort read of the configured run hour without an async context (scheduler setup).
    Interpreted on the server's local clock — APScheduler's default timezone."""
    import sqlite3
    from ..config import settings
    try:
        con = sqlite3.connect(settings.ego_db_path)
        row = con.execute("SELECT value FROM app_settings WHERE key = 'reflection_hour'").fetchone()
        con.close()
        return max(0, min(23, int(row[0]))) if row else 4
    except Exception:
        return 4


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
    # Nightly reflection pass at the configured server-local hour: conclusions, day-mood, dreams.
    scheduler.add_job(
        _run_reflections_job,
        "cron",
        hour=_reflection_hour(),
        minute=0,
        id="run_reflections",
        max_instances=1,
        coalesce=True,
    )
    # Phase 5 — outward reply attribution: classify each reach-out landed/ignored once its reply window
    # has closed, feeding the lonely-sting + outward backoff.
    from .impulse_feedback import attribute_outward
    scheduler.add_job(
        attribute_outward,
        "interval",
        minutes=5,
        id="attribute_outward",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
