"""Time Injector — injects current date/time in LOCAL timezone."""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def inject_time(**kwargs):
    try:
        now = datetime.now().astimezone()
        ts = now.strftime('%A, %B %d, %Y — %I:%M:%S %p %Z')
        return {"context": f"[CURRENT DATETIME — {ts}]"}
    except Exception as e:
        logger.warning(f"Time injector failed: {e}")

def register(ctx):
    logger.info("Time Injector registered — using astimezone()")
    ctx.register_hook("pre_llm_call", inject_time)
