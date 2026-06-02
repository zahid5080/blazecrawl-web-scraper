# blazecrawl/services/cache.py
"""Async-safe in-memory TTL cache with size cap and background cleanup.

A Redis backend can be added later by swapping the underlying store; the
public interface (``get`` / ``set`` / ``delete`` / ``clear``) is the same.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Async-safe in-memory cache with per-entry TTL.

    Concurrency model: a single ``asyncio.Lock`` guards all mutations.  We
    additionally run a periodic cleanup task that evicts expired entries.
    """

    def __init__(
        self,
        ttl: Optional[int] = None,
        max_size: Optional[int] = None,
        cleanup_interval: float = 60.0,
    ) -> None:
        self._ttl = ttl if ttl is not None else settings.CACHE_TTL
        self._max_size = max_size if max_size is not None else settings.CACHE_MAX_SIZE
        self._cleanup_interval = cleanup_interval
        self._store: Dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._closed = False

    # ------------------------------------------------------------------ API
    async def get(self, key: str) -> Optional[Any]:
        """Return cached value or ``None`` if absent/expired."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                self._store.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Insert *value* under *key* with optional per-call TTL override."""
        effective_ttl = ttl if ttl is not None else self._ttl
        async with self._lock:
            if len(self._store) >= self._max_size and key not in self._store:
                # Evict the oldest (FIFO-ish) entry.
                try:
                    oldest_key = next(iter(self._store))
                    self._store.pop(oldest_key, None)
                except StopIteration:
                    pass
            self._store[key] = _Entry(
                value=value, expires_at=time.monotonic() + effective_ttl
            )

    async def delete(self, key: str) -> bool:
        """Remove *key* if present; return whether something was removed."""
        async with self._lock:
            return self._store.pop(key, None) is not None

    async def clear(self) -> None:
        """Remove all entries."""
        async with self._lock:
            self._store.clear()

    async def size(self) -> int:
        """Return current number of (possibly expired) entries."""
        async with self._lock:
            return len(self._store)

    # -------------------------------------------------------- maintenance
    async def _cleanup_loop(self) -> None:
        """Background loop: every ``cleanup_interval`` seconds purge expired."""
        try:
            while not self._closed:
                await asyncio.sleep(self._cleanup_interval)
                now = time.monotonic()
                removed = 0
                async with self._lock:
                    expired_keys = [
                        k for k, v in self._store.items() if v.expires_at < now
                    ]
                    for k in expired_keys:
                        self._store.pop(k, None)
                        removed += 1
                if removed:
                    logger.debug("cache cleanup removed %s expired entries", removed)
        except asyncio.CancelledError:
            logger.debug("cache cleanup task cancelled")
            raise

    def start(self) -> None:
        """Start the background cleanup task (idempotent)."""
        if self._cleanup_task is None or self._cleanup_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no running loop yet — caller will start later
            self._cleanup_task = loop.create_task(
                self._cleanup_loop(), name="cache-cleanup"
            )

    async def stop(self) -> None:
        """Stop the cleanup task and clear the cache."""
        self._closed = True
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover
                logger.warning("cache cleanup task raised on shutdown: %s", exc)
        await self.clear()


# Process-global cache instance (importable by other modules).
cache = TTLCache()
