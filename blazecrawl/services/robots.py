# blazecrawl/services/robots.py
"""Async robots.txt fetcher, parser, and compliance checker.

Each domain's parsed robots.txt is cached for the process lifetime.  Fetch
errors are cached as "permissive" parsers so we don't repeatedly hammer
sites that don't serve a robots.txt.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from config import settings
from utils.url import get_base_url, get_domain

logger = logging.getLogger(__name__)


class RobotsChecker:
    """Robots.txt fetcher + cache + helper API."""

    def __init__(self, user_agent: Optional[str] = None) -> None:
        self._ua = user_agent or settings.DEFAULT_USER_AGENT
        self._cache: Dict[str, RobotFileParser] = {}
        self._sitemaps: Dict[str, List[str]] = {}
        self._fetch_locks: Dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _get_lock(self, domain: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._fetch_locks.get(domain)
            if lock is None:
                lock = asyncio.Lock()
                self._fetch_locks[domain] = lock
            return lock

    async def _fetch_robots(self, base: str) -> Tuple[RobotFileParser, List[str]]:
        """Fetch and parse ``{base}/robots.txt``.  Returns ``(parser, sitemaps)``."""
        robots_url = f"{base.rstrip('/')}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        sitemaps: List[str] = []
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": self._ua},
                follow_redirects=True,
            ) as client:
                resp = await client.get(robots_url)
            if resp.status_code >= 400:
                logger.debug(
                    "robots.txt for %s returned %s — treating as permissive",
                    base, resp.status_code,
                )
                parser.parse([])
            else:
                text = resp.text or ""
                parser.parse(text.splitlines())
                # extract sitemap directives manually (urllib doesn't surface them)
                for line in text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        _, _, sm = line.partition(":")
                        sm = sm.strip()
                        if sm:
                            sitemaps.append(sm)
        except Exception as exc:
            logger.debug("robots.txt fetch failed for %s: %s", base, exc)
            parser.parse([])  # permissive default
        return parser, sitemaps

    async def get_parser(self, url: str) -> RobotFileParser:
        """Return a cached :class:`RobotFileParser` for *url*'s domain."""
        domain = get_domain(url)
        if not domain:
            # If we can't extract a domain, return a permissive parser.
            rp = RobotFileParser()
            rp.parse([])
            return rp

        parser = self._cache.get(domain)
        if parser is not None:
            return parser

        lock = await self._get_lock(domain)
        async with lock:
            parser = self._cache.get(domain)
            if parser is not None:
                return parser
            base = get_base_url(url)
            parser, sitemaps = await self._fetch_robots(base)
            self._cache[domain] = parser
            self._sitemaps[domain] = sitemaps
            return parser

    async def can_fetch(self, url: str) -> bool:
        """Return ``True`` if *url* may be fetched per robots.txt."""
        parser = await self.get_parser(url)
        try:
            return parser.can_fetch(self._ua, url)
        except Exception:  # pragma: no cover - parser bug fallback
            return True

    async def crawl_delay(self, url: str) -> Optional[float]:
        """Return the ``Crawl-delay`` (seconds) declared for our UA, if any."""
        parser = await self.get_parser(url)
        try:
            delay = parser.crawl_delay(self._ua)
            return float(delay) if delay is not None else None
        except Exception:
            return None

    async def sitemaps(self, url: str) -> List[str]:
        """Return any sitemap URLs declared in robots.txt for *url*'s domain."""
        await self.get_parser(url)  # ensure populated
        return list(self._sitemaps.get(get_domain(url), []))


# Process-global checker (importable by other modules).
robots_checker = RobotsChecker()
