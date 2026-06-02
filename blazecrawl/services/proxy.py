# blazecrawl/services/proxy.py
"""Round-robin proxy rotation.

If no proxies are configured the manager returns ``None`` from
:meth:`next_proxy`, which httpx interprets as a direct connection.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Iterable, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class ProxyManager:
    """Async-safe round-robin proxy rotator."""

    def __init__(self, proxies: Optional[Iterable[str]] = None) -> None:
        self._proxies: List[str] = list(proxies) if proxies else list(settings.PROXIES)
        self._lock = asyncio.Lock()
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None
        if self._proxies:
            logger.info("proxy rotation enabled with %s proxies", len(self._proxies))
        else:
            logger.info("no proxies configured — direct connections only")

    async def next_proxy(self) -> Optional[str]:
        """Return the next proxy URL or ``None`` if none are configured."""
        if not self._proxies or self._cycle is None:
            return None
        async with self._lock:
            return next(self._cycle)

    async def add_proxy(self, proxy: str) -> None:
        """Append a proxy to the rotation pool (thread-safely)."""
        async with self._lock:
            self._proxies.append(proxy)
            self._cycle = itertools.cycle(self._proxies)

    async def remove_proxy(self, proxy: str) -> bool:
        """Remove a proxy by URL; return whether something was removed."""
        async with self._lock:
            if proxy in self._proxies:
                self._proxies.remove(proxy)
                self._cycle = itertools.cycle(self._proxies) if self._proxies else None
                return True
            return False

    @property
    def has_proxies(self) -> bool:
        """``True`` if at least one proxy is configured."""
        return bool(self._proxies)

    @property
    def count(self) -> int:
        """Number of proxies in the pool."""
        return len(self._proxies)


# Process-global manager (importable by other modules).
proxy_manager = ProxyManager()
