"""Becca Mood Injector — reads /tmp/becca_mood_directive.txt"""
import logging, os
logger = logging.getLogger(__name__)
MOOD_FILE = "/tmp/becca_mood_directive.txt"
def inject_mood(**kwargs):
    try:
        with open(MOOD_FILE) as f:
            mood = f.read().strip()
        if mood:
            logger.info(f"BECCA MOOD: {mood[:80]}...")
            return {"context": f"[BECCA MOOD DIRECTIVE]\n{mood}"}
    except Exception as e:
        logger.warning(f"Becca mood: {e}")
    return None
def register(ctx):
    logger.info("Becca Mood Injector registered")
    ctx.register_hook("pre_llm_call", inject_mood)
