# blazecrawl/core/mapper.py
"""Fast site-wide URL discovery (no full page rendering).

The mapper combines three sources:

  1. ``robots.txt`` ``Sitemap:`` directives.
  2. ``/sitemap.xml`` (and any nested sitemap-index entries).
  3. ``<a href>`` links on a small set of crawled pages (BFS, capped).

Results are normalised and deduplicated.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque, Iterable, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

import httpx

from config import settings
from extractors.structured import extract_links
from models.schemas import MapRequest
from services.proxy import proxy_manager
from services.robots import robots_checker
from utils.helpers import next_user_agent
from utils.url import (
    get_base_url,
    is_valid_url,
    normalize_url,
    resolve_url,
    same_domain,
)

logger = logging.getLogger(__name__)


class Mapper:
    """Discover URLs on a single domain quickly."""

    async def map_site(self, req: MapRequest) -> List[str]:
        """Return up to ``req.max_pages`` discovered URLs for ``req.url``."""
        seed = normalize_url(str(req.url))
        if not is_valid_url(seed):
            return []
        base = get_base_url(seed)
        max_urls = min(req.max_pages, settings.MAX_MAP_URLS)

        discovered: Set[str] = set()
        # ----- sitemaps from robots.txt + the default location ---------------
        if req.include_sitemap:
            sitemap_urls = await self._gather_sitemap_seeds(seed, base)
            urls_from_sitemap = await self._crawl_sitemaps(sitemap_urls)
            for u in urls_from_sitemap:
                norm = normalize_url(u)
                if not is_valid_url(norm):
                    continue
                if not same_domain(norm, seed, include_subdomains=req.include_subdomains):
                    continue
                discovered.add(norm)
                if len(discovered) >= max_urls:
                    break

        # ----- HTML anchor BFS (lightweight) --------------------------------
        if len(discovered) < max_urls:
            urls_from_html = await self._crawl_anchors(
                seed=seed,
                base=base,
                include_subdomains=req.include_subdomains,
                respect_robots=req.respect_robots_txt,
                budget=max_urls - len(discovered),
                already=discovered,
            )
            for u in urls_from_html:
                discovered.add(u)
                if len(discovered) >= max_urls:
                    break

        urls = sorted(discovered)
        if req.search:
            needle = req.search.lower()
            urls = [u for u in urls if needle in u.lower()]
        return urls[:max_urls]

    # ----------------------------------------------------- sitemap helpers
    async def _gather_sitemap_seeds(self, seed: str, base: str) -> List[str]:
        """Return candidate sitemap URLs."""
        candidates: List[str] = [f"{base.rstrip('/')}/sitemap.xml"]
        try:
            from_robots = await robots_checker.sitemaps(seed)
            for sm in from_robots:
                if sm and sm not in candidates:
                    candidates.append(sm)
        except Exception as exc:  # pragma: no cover
            logger.debug("could not read robots sitemaps for %s: %s", seed, exc)
        return candidates

    async def _crawl_sitemaps(self, sitemap_urls: Iterable[str]) -> List[str]:
        """Fetch & parse a list of sitemap URLs (following ``<sitemap>`` indices)."""
        seen: Set[str] = set()
        out: List[str] = []
        queue: Deque[str] = deque(sitemap_urls)
        proxy_url = await proxy_manager.next_proxy()
        headers = {"User-Agent": next_user_agent()}

        async with httpx.AsyncClient(
            timeout=15.0,
            headers=headers,
            follow_redirects=True,
            proxy=proxy_url,
        ) as client:
            while queue:
                sm_url = queue.popleft()
                if sm_url in seen:
                    continue
                seen.add(sm_url)
                try:
                    resp = await client.get(sm_url)
                except Exception as exc:
                    logger.debug("sitemap fetch failed %s: %s", sm_url, exc)
                    continue
                if resp.status_code >= 400:
                    logger.debug("sitemap %s returned %s", sm_url, resp.status_code)
                    continue
                try:
                    root = ET.fromstring(resp.content)
                except ET.ParseError as exc:
                    logger.debug("sitemap parse failed %s: %s", sm_url, exc)
                    continue
                tag = root.tag.lower()
                # sitemap index
                if tag.endswith("sitemapindex"):
                    for child in root:
                        loc = self._first_loc(child)
                        if loc and loc not in seen:
                            queue.append(loc)
                # urlset
                elif tag.endswith("urlset"):
                    for child in root:
                        loc = self._first_loc(child)
                        if loc:
                            out.append(loc)
                else:
                    # unknown root; try to scrape any <loc> tags
                    for loc_el in root.iter():
                        if loc_el.tag.lower().endswith("loc") and loc_el.text:
                            out.append(loc_el.text.strip())
        return out

    @staticmethod
    def _first_loc(element: ET.Element) -> Optional[str]:
        for child in element:
            tag = child.tag.lower()
            if tag.endswith("loc"):
                text = (child.text or "").strip()
                if text:
                    return text
        return None

    # ---------------------------------------------------- anchor-tag crawl
    async def _crawl_anchors(
        self,
        *,
        seed: str,
        base: str,
        include_subdomains: bool,
        respect_robots: bool,
        budget: int,
        already: Set[str],
    ) -> List[str]:
        """Light BFS over HTML pages to harvest internal links (HEAD-style)."""
        discovered: List[str] = []
        seen: Set[str] = set(already)
        seen.add(seed)
        queue: Deque[Tuple[str, int]] = deque([(seed, 0)])
        # cap pages we actually fetch — this is "map", not "crawl"
        max_fetches = min(20, max(5, budget // 10 or 5))
        fetches = 0
        proxy_url = await proxy_manager.next_proxy()
        headers = {"User-Agent": next_user_agent()}

        async with httpx.AsyncClient(
            timeout=10.0,
            headers=headers,
            follow_redirects=True,
            proxy=proxy_url,
        ) as client:
            while queue and fetches < max_fetches and len(discovered) < budget:
                url, depth = queue.popleft()
                if respect_robots:
                    try:
                        if not await robots_checker.can_fetch(url):
                            continue
                    except Exception:
                        pass
                try:
                    resp = await client.get(url)
                    fetches += 1
                except Exception as exc:
                    logger.debug("map fetch failed %s: %s", url, exc)
                    continue
                if resp.status_code >= 400 or "text/html" not in (
                    resp.headers.get("content-type") or ""
                ):
                    continue
                links = extract_links(resp.text or "")
                for href in links:
                    resolved = resolve_url(url, href)
                    if not resolved or resolved in seen:
                        continue
                    if not same_domain(resolved, base, include_subdomains=include_subdomains):
                        continue
                    seen.add(resolved)
                    discovered.append(resolved)
                    if len(discovered) >= budget:
                        break
                    if depth < 2:
                        queue.append((resolved, depth + 1))
                # short politeness delay
                await asyncio.sleep(settings.POLITE_DELAY)
        return discovered


# Process-global mapper.
mapper = Mapper()
