# blazecrawl/services/rate_limiter.py
"""Per-domain async token-bucket rate limiter.

The bucket fills at ``rate`` tokens/second up to ``burst`` capacity.  Every
:meth:`acquire` consumes one token; when the bucket is empty the coroutine
sleeps until enough tokens have accumulated.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from config import settings
from utils.url import get_domain

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    rate: float                      # tokens / second
    capacity: float                  # max tokens
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self.last_refill
        if delta > 0:
            self.tokens = min(self.capacity, self.tokens + delta * self.rate)
            self.last_refill = now


class RateLimiter:
    """Async token-bucket rate limiter keyed by domain."""

    def __init__(
        self,
        default_rps: Optional[float] = None,
        default_burst: Optional[int] = None,
    ) -> None:
        self._default_rps = (
            default_rps if default_rps is not None else settings.DEFAULT_RPS
        )
        self._default_burst = (
            default_burst if default_burst is not None else settings.DEFAULT_BURST
        )
        self._buckets: Dict[str, _Bucket] = {}
        self._registry_lock = asyncio.Lock()

    async def _get_or_create(self, domain: str) -> _Bucket:
        async with self._registry_lock:
            bucket = self._buckets.get(domain)
            if bucket is None:
                bucket = _Bucket(
                    rate=self._default_rps,
                    capacity=float(self._default_burst),
                    tokens=float(self._default_burst),
                )
                self._buckets[domain] = bucket
            return bucket

    async def acquire(self, url_or_domain: str) -> None:
        """Block until a token is available for *url_or_domain*."""
        domain = get_domain(url_or_domain) or url_or_domain
        bucket = await self._get_or_create(domain)
        async with bucket.lock:
            while True:
                bucket._refill()
                if bucket.tokens >= 1:
                    bucket.tokens -= 1
                    return
                # need (1 - tokens) more tokens; sleep just enough
                needed = 1 - bucket.tokens
                wait_for = max(needed / bucket.rate, 0.01)
                logger.debug(
                    "rate-limit sleep %.3fs for %s", wait_for, domain
                )
                await asyncio.sleep(wait_for)

    async def configure_domain(
        self,
        domain: str,
        rate: Optional[float] = None,
        burst: Optional[int] = None,
    ) -> None:
        """Override the bucket parameters for a specific domain.

        Useful e.g. when robots.txt declares a ``Crawl-delay`` directive.
        """
        domain = get_domain(domain) or domain
        async with self._registry_lock:
            bucket = self._buckets.get(domain)
            new_rate = rate if rate is not None else self._default_rps
            new_capacity = float(burst) if burst is not None else float(
                self._default_burst
            )
            if bucket is None:
                self._buckets[domain] = _Bucket(
                    rate=new_rate,
                    capacity=new_capacity,
                    tokens=new_capacity,
                )
            else:
                bucket.rate = new_rate
                bucket.capacity = new_capacity
                bucket.tokens = min(bucket.tokens, new_capacity)


# Process-global rate-limiter (importable by other modules).
rate_limiter = RateLimiter()
