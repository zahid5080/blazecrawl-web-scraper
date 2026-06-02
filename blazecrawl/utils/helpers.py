# blazecrawl/utils/helpers.py
"""Reusable helpers: retries, user-agent rotation, hashing, misc."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import itertools
import logging
import random
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Tuple, Type, TypeVar

from config import USER_AGENTS, settings

logger = logging.getLogger(__name__)

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


# --------------------------------------------------------------------------- #
# Retry decorator
# --------------------------------------------------------------------------- #
def async_retry(
    max_retries: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: float = 30.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    jitter: bool = True,
) -> Callable[[F], F]:
    """Retry an async function with exponential backoff.

    Parameters mirror typical retry helpers; defaults are pulled from
    :data:`config.settings` when not supplied explicitly.

    Example::

        @async_retry(max_retries=3, base_delay=0.5)
        async def fetch(...): ...
    """
    retries = max_retries if max_retries is not None else settings.MAX_RETRIES
    delay = base_delay if base_delay is not None else settings.RETRY_BASE_DELAY

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            last_exc: Optional[BaseException] = None
            while attempt <= retries:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203 - retry loop is fine
                    last_exc = exc
                    if attempt == retries:
                        break
                    wait_for = min(delay * (2 ** attempt), max_delay)
                    if jitter:
                        wait_for *= 0.5 + random.random()
                    logger.warning(
                        "retry %s/%s for %s after %.2fs (%s: %s)",
                        attempt + 1,
                        retries,
                        getattr(func, "__name__", "anon"),
                        wait_for,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(wait_for)
                    attempt += 1
            assert last_exc is not None  # for type-checkers
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# --------------------------------------------------------------------------- #
# User-agent rotation
# --------------------------------------------------------------------------- #
class UserAgentRotator:
    """Round-robin rotation of realistic browser user-agent strings."""

    def __init__(self, agents: Optional[Iterable[str]] = None) -> None:
        pool = list(agents) if agents else list(USER_AGENTS)
        if not pool:
            pool = [settings.DEFAULT_USER_AGENT]
        self._pool = pool
        self._cycle = itertools.cycle(self._pool)

    def next(self) -> str:
        """Return the next user-agent string."""
        return next(self._cycle)

    def random(self) -> str:
        """Return a random user-agent string."""
        return random.choice(self._pool)


_global_rotator = UserAgentRotator()


def next_user_agent() -> str:
    """Return the next rotated user-agent string (process-global)."""
    return _global_rotator.next()


def random_user_agent() -> str:
    """Return a random user-agent string (process-global)."""
    return _global_rotator.random()


# --------------------------------------------------------------------------- #
# Hashing & dedup
# --------------------------------------------------------------------------- #
def content_hash(content: str | bytes) -> str:
    """Return a hex SHA-256 hash of *content* for deduplication."""
    if isinstance(content, str):
        content = content.encode("utf-8", errors="ignore")
    return hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
def now_ms() -> float:
    """Return monotonic time in milliseconds."""
    return time.monotonic() * 1000.0


def truncate(s: Optional[str], max_chars: int = 200) -> str:
    """Truncate *s* to ``max_chars`` characters, appending ``…``."""
    if s is None:
        return ""
    s = s.strip()
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


def word_count(text: Optional[str]) -> int:
    """Return a rough word count of *text* (whitespace-split)."""
    if not text:
        return 0
    return len(text.split())


def safe_int(value: Any, default: int = 0) -> int:
    """Best-effort ``int(value)`` with a fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
