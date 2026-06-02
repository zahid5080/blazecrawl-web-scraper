# blazecrawl/core/crawler.py
"""Recursive BFS crawler with async job tracking.

Job lifecycle:

  * ``POST /crawl``     → :meth:`Crawler.start_job` → returns ``job_id`` immediately.
  * Background task     → :meth:`Crawler._run_job` walks the site.
  * ``GET  /crawl/{id}`` → :meth:`Crawler.get_job`  → returns the current snapshot.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Set, Tuple

from config import settings
from core.scraper import ScrapeError, scraper
from extractors.structured import extract_links
from models.schemas import (
    CrawlJobResponse,
    CrawlRequest,
    JobStatus,
    OutputFormat,
    PageData,
    ScrapeRequest,
)
from utils.url import (
    get_domain,
    is_filtered,
    is_valid_url,
    normalize_url,
    resolve_url,
    same_domain,
)

logger = logging.getLogger(__name__)


class _Job:
    """Internal mutable record for a crawl job."""

    def __init__(self, job_id: str, req: CrawlRequest) -> None:
        self.id = job_id
        self.req = req
        self.status: JobStatus = JobStatus.PENDING
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.error: Optional[str] = None
        self.pages: List[PageData] = []
        self.pages_failed: int = 0
        self.task: Optional[asyncio.Task[None]] = None
        self.lock = asyncio.Lock()

    def snapshot(self) -> CrawlJobResponse:
        return CrawlJobResponse(
            job_id=self.id,
            status=self.status,
            total_pages=len(self.pages),
            pages_crawled=len(self.pages),
            pages_failed=self.pages_failed,
            seed_url=str(self.req.url),
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
            data=list(self.pages),
        )


class Crawler:
    """Recursive crawler with in-memory job tracking."""

    def __init__(self) -> None:
        self._jobs: Dict[str, _Job] = {}
        self._jobs_lock = asyncio.Lock()

    # ------------------------------------------------------------------ API
    async def start_job(self, req: CrawlRequest) -> str:
        """Register a new crawl job and kick off its background task."""
        job_id = f"crawl_{secrets.token_hex(6)}"
        job = _Job(job_id, req)
        async with self._jobs_lock:
            self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run_job(job), name=f"crawl-{job_id}")
        return job_id

    async def get_job(self, job_id: str) -> Optional[CrawlJobResponse]:
        """Return the public snapshot of a job, or ``None`` if not found."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        async with job.lock:
            return job.snapshot()

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job; return True on success."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.task and not job.task.done():
            job.task.cancel()
            return True
        return False

    async def active_count(self) -> int:
        """Return the number of running/pending jobs."""
        return sum(
            1 for j in self._jobs.values()
            if j.status in {JobStatus.PENDING, JobStatus.RUNNING}
        )

    # ----------------------------------------------------------- crawl loop
    async def _run_job(self, job: _Job) -> None:
        """Execute the BFS crawl for *job*."""
        req = job.req
        seed = normalize_url(str(req.url))
        if not is_valid_url(seed):
            job.status = JobStatus.FAILED
            job.error = f"invalid seed URL: {req.url}"
            job.finished_at = datetime.now(timezone.utc)
            return

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)

        max_pages = min(req.max_pages, settings.MAX_CRAWL_PAGES)
        max_depth = min(req.max_depth, settings.MAX_CRAWL_DEPTH)
        concurrency = req.concurrency or settings.CRAWLER_CONCURRENCY
        semaphore = asyncio.Semaphore(concurrency)

        visited: Set[str] = set()
        queue: Deque[Tuple[str, int]] = deque([(seed, 0)])
        in_flight: Set[asyncio.Task[None]] = set()
        last_hit_time: Dict[str, float] = {}

        async def _process(url: str, depth: int) -> None:
            """Scrape a single URL and enqueue its outgoing links."""
            try:
                async with semaphore:
                    # Polite delay between hits to the same domain.
                    domain_key = get_domain(url) or url
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    last = last_hit_time.get(domain_key, 0.0)
                    delta = now - last
                    if delta < settings.POLITE_DELAY:
                        await asyncio.sleep(settings.POLITE_DELAY - delta)
                    last_hit_time[domain_key] = loop.time()

                    scrape_req = ScrapeRequest(
                        url=url,  # type: ignore[arg-type]
                        formats=req.formats or [OutputFormat.MARKDOWN],
                        only_main_content=req.only_main_content,
                        include_metadata=req.include_metadata,
                        include_structured=req.include_structured,
                        include_links=True,
                        wait_for_js=req.wait_for_js,
                        timeout=settings.BROWSER_TIMEOUT,
                        headers=req.headers or {},
                        remove_selectors=req.remove_selectors or [],
                        respect_robots_txt=req.respect_robots_txt,
                    )
                    page = await scraper.scrape(scrape_req)
            except ScrapeError as exc:
                logger.info("scrape failed for %s: %s", url, exc)
                async with job.lock:
                    job.pages_failed += 1
                    job.pages.append(PageData(url=url, error=str(exc)))
                return
            except Exception as exc:
                logger.warning("unexpected error scraping %s: %s", url, exc)
                async with job.lock:
                    job.pages_failed += 1
                    job.pages.append(PageData(url=url, error=str(exc)))
                return

            async with job.lock:
                if len(job.pages) >= max_pages:
                    return
                job.pages.append(page)

            # Enqueue children if we have room and aren't at max depth.
            if depth >= max_depth:
                return

            # Use the (possibly redirected) final URL as the link base.
            base = page.url or url
            for href in (page.links or []):
                resolved = resolve_url(base, href)
                if not resolved:
                    continue
                if not same_domain(resolved, seed, include_subdomains=False):
                    continue
                if is_filtered(
                    resolved,
                    allow_patterns=req.allow_patterns or (),
                    deny_patterns=req.deny_patterns or (),
                ):
                    continue
                if resolved in visited:
                    continue
                visited.add(resolved)
                queue.append((resolved, depth + 1))

        try:
            visited.add(seed)
            while queue or in_flight:
                async with job.lock:
                    pages_so_far = len(job.pages)
                if pages_so_far >= max_pages:
                    break

                # Spawn workers up to concurrency.
                while queue and len(in_flight) < concurrency:
                    async with job.lock:
                        scheduled = len(job.pages) + len(in_flight)
                    if scheduled >= max_pages:
                        break
                    url, depth = queue.popleft()
                    task = asyncio.create_task(_process(url, depth))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)

                if not in_flight:
                    break

                # Wait for at least one task to complete before scheduling more.
                done, _ = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    # propagate cancellations from the outer job
                    if t.cancelled():
                        raise asyncio.CancelledError
                    exc = t.exception()
                    if exc is not None and not isinstance(exc, Exception):
                        raise exc  # pragma: no cover

            # drain any remaining in-flight after page-cap or queue empty
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            job.status = JobStatus.COMPLETED
            logger.info(
                "crawl %s completed: %s ok, %s failed",
                job.id, len(job.pages) - job.pages_failed, job.pages_failed,
            )
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.error = "cancelled"
            logger.info("crawl %s cancelled", job.id)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            logger.exception("crawl %s failed: %s", job.id, exc)
        finally:
            job.finished_at = datetime.now(timezone.utc)


# Process-global crawler.
crawler = Crawler()
