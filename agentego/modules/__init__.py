from typing import Callable, List
import logging

logger = logging.getLogger("agentego.modules")

_event_handlers: List[Callable] = []


def register_event_handler(fn: Callable) -> Callable:
    """Decorator — register an async function to receive every HookEvent."""
    _event_handlers.append(fn)
    return fn


async def dispatch_event(event) -> None:
    for handler in _event_handlers:
        try:
            await handler(event)
        except Exception as exc:
            logger.warning("Module handler %s failed: %s", handler.__name__, exc)


def load_modules(app) -> None:
    """Called at startup. Import and register modules here as they are added."""
    pass
