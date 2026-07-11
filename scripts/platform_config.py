"""
Shared platform configuration — single source of truth for active chat platforms.

Change ACTIVE_SOURCES here and every script/plugin follows automatically.
Use source_filter() from bash scripts: $(python3 -c "from platform_config import source_filter; print(source_filter())")
"""
import re

# Platforms the agents are currently active on.
# Format: SQL-trusted tuple for IN (...) clauses.
ACTIVE_SOURCES = ('telegram', 'discord')


def source_filter() -> str:
    """Return a SQL-safe IN clause string for bash scripts.
    Example: "source IN ('telegram','discord')"
    """
    quoted = ','.join(repr(s) for s in ACTIVE_SOURCES)
    return f'source IN ({quoted})'


def source_clause() -> str:
    """Return the full WHERE fragment for Python SQL queries."""
    return source_filter()
