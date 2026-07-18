"""Conservative, dependency-free pacing for anonymous Reddit HTTP requests."""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

DEFAULT_MIN_INTERVAL = 6.0
DEFAULT_JITTER_MAX = 1.0
DEFAULT_429_DELAY = 60.0
MAX_429_ATTEMPTS = 2


class RedditRateLimited(RuntimeError):
    """Raised when Reddit continues returning 429 after the one allowed retry."""


class RedditRequestGate:
    """Spaces requests and provides deterministic clock/sleep injection for tests."""

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        jitter_max: float = DEFAULT_JITTER_MAX,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.min_interval = min_interval
        self.jitter_max = jitter_max
        self._clock = clock
        self._sleeper = sleeper
        self._random_uniform = random_uniform
        self._last_request_at: float | None = None

    def wait_for_slot(self) -> None:
        """Wait until this process may start its next Reddit request."""
        now = self._clock()
        if self._last_request_at is None:
            self._last_request_at = now
            return

        jitter = self._random_uniform(0.0, self.jitter_max)
        next_allowed = self._last_request_at + self.min_interval + jitter
        delay = max(0.0, next_allowed - now)
        if delay:
            self._sleeper(delay)
        self._last_request_at = max(self._clock(), next_allowed)

    def defer(self, seconds: float) -> None:
        """Apply a server-requested cooldown before the next attempted request."""
        delay = max(0.0, seconds)
        if delay:
            self._sleeper(delay)
        self._last_request_at = self._clock()


def retry_after_seconds(headers) -> float:
    """Return a safe cooldown from Retry-After seconds or an HTTP-date."""
    if not headers:
        return DEFAULT_429_DELAY

    value = headers.get("Retry-After")
    if not value:
        return DEFAULT_429_DELAY

    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError, OverflowError):
        return DEFAULT_429_DELAY


def reddit_open(request, *, timeout: float, gate: RedditRequestGate, opener=urllib.request.urlopen):
    """Open one Reddit request, with paced start and one 429-aware retry."""
    if isinstance(request, str):
        request = urllib.request.Request(request)

    for attempt in range(MAX_429_ATTEMPTS):
        gate.wait_for_slot()
        try:
            return opener(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise

            delay = retry_after_seconds(error.headers)
            if attempt + 1 >= MAX_429_ATTEMPTS:
                raise RedditRateLimited(
                    f"Reddit kept rate-limiting {request.full_url} after one retry "
                    f"(last requested cooldown: {delay:.0f}s)."
                ) from error
            gate.defer(delay)

    raise AssertionError("Unreachable retry loop exit")
