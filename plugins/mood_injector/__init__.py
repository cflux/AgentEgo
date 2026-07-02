"""
Tala Mood Injector Plugin
Reads live mood directive from AgentEgo and injects it into the system prompt
before every LLM call — no model-level file reads needed.
"""
import logging
import os

logger = logging.getLogger(__name__)
MOOD_FILE = "/tmp/tala_mood_directive.txt"


def inject_mood(**kwargs):
    """pre_llm_call hook: read mood file, inject as context."""
    try:
        logger.debug(f"inject_mood called with: {list(kwargs.keys())}")
        with open(MOOD_FILE, "r") as f:
            mood = f.read().strip()
        if mood:
            logger.info(f"MOOD INJECTED: {mood[:80]}...")
            return {"context": f"[MOOD DIRECTIVE — apply this to your tone and emotional state this turn]\n{mood}"}
        else:
            logger.debug("Mood file empty, skipping injection")
    except Exception as e:
        logger.warning(f"Mood injector failed: {e}")
    return None  # silent no-op if file missing


def register(ctx):
    logger.info("Mood Injector plugin registered — pre_llm_call hook active")
    ctx.register_hook("pre_llm_call", inject_mood)
