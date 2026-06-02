# blazecrawl/main.py
"""FastAPI entry point for BlazeCrawl.

Run with::

    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

# --- sys.path bootstrap ---------------------------------------------------
# Ensure the directory containing this file is on sys.path so that local
# packages (`core`, `models`, `services`, etc.) resolve correctly regardless
# of how the app is launched (e.g. `uvicorn main:app`, `python main.py`,
# or a Windows console_script `uvicorn.exe`, which does NOT add the current
# working directory to sys.path).
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# --------------------------------------------------------------------------

import asyncio
import logging
import signal
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from config import configure_logging, settings
from core.browser import browser_manager
from core.crawler import crawler
from core.mapper import mapper
from core.scraper import ScrapeError, scraper
from models.schemas import (
    CrawlJobResponse,
    CrawlRequest,
    CrawlStartResponse,
    ErrorResponse,
    HealthResponse,
    JobStatus,
    MapRequest,
    MapResponse,
    ScrapeRequest,
    ScrapeResponse,
)
from services.cache import cache

configure_logging()
logger = logging.getLogger("blazecrawl")

# Process-start timestamp for the /health uptime field.
_started_at = time.monotonic()


# --------------------------------------------------------------------------- #
# Lifespan: start cache, optionally pre-warm browser, then teardown.
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: warm-up & graceful shutdown."""
    logger.info("BlazeCrawl starting up")
    cache.start()

    # Install SIGTERM handler for clean shutdown when run under e.g. Docker.
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown(*_: object) -> None:
        if not shutdown_event.is_set():
            logger.info("shutdown signal received")
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread → fall back silently
            pass

    try:
        yield
    finally:
        logger.info("BlazeCrawl shutting down")
        # Cancel any in-flight crawl tasks.
        try:
            pending = [
                j.task for j in crawler._jobs.values()  # noqa: SLF001
                if j.task is not None and not j.task.done()
            ]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:  # pragma: no cover
            logger.debug("error cancelling crawl tasks: %s", exc)

        try:
            await cache.stop()
        except Exception as exc:  # pragma: no cover
            logger.debug("error stopping cache: %s", exc)

        try:
            await browser_manager.stop()
        except Exception as exc:  # pragma: no cover
            logger.debug("error stopping browser: %s", exc)

        logger.info("BlazeCrawl stopped cleanly")


app = FastAPI(
    title="BlazeCrawl",
    description="A FireCrawl-style web scraper & crawler.",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Global exception handler — never crash, always return a JSON envelope.
# --------------------------------------------------------------------------- #
@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_server_error", detail=str(exc)
        ).model_dump(),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post(
    "/scrape",
    response_model=ScrapeResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def scrape_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    """Scrape a single URL and return its content in the requested formats."""
    logger.info("POST /scrape url=%s js=%s", req.url, req.wait_for_js)
    try:
        data = await scraper.scrape(req)
    except ScrapeError as exc:
        logger.info("scrape error for %s: %s", req.url, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("scrape internal error for %s", req.url)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ScrapeResponse(success=True, data=data)


@app.post(
    "/crawl",
    response_model=CrawlStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def crawl_start_endpoint(req: CrawlRequest) -> CrawlStartResponse:
    """Kick off an asynchronous recursive crawl and return its ``job_id``."""
    logger.info(
        "POST /crawl url=%s depth=%s max_pages=%s js=%s",
        req.url, req.max_depth, req.max_pages, req.wait_for_js,
    )
    try:
        job_id = await crawler.start_job(req)
    except Exception as exc:
        logger.exception("failed to start crawl job: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return CrawlStartResponse(success=True, job_id=job_id, status=JobStatus.RUNNING)


@app.get(
    "/crawl/{job_id}",
    response_model=CrawlJobResponse,
    responses={404: {"model": ErrorResponse}},
)
async def crawl_status_endpoint(job_id: str) -> CrawlJobResponse:
    """Return the current status & accumulated data for a crawl job."""
    snap = await crawler.get_job(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return snap


@app.delete(
    "/crawl/{job_id}",
    response_model=CrawlJobResponse,
    responses={404: {"model": ErrorResponse}},
)
async def crawl_cancel_endpoint(job_id: str) -> CrawlJobResponse:
    """Cancel a running crawl job."""
    ok = await crawler.cancel_job(job_id)
    snap = await crawler.get_job(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if not ok:
        logger.info("crawl %s already finished — nothing to cancel", job_id)
    return snap


@app.post("/map", response_model=MapResponse)
async def map_endpoint(req: MapRequest) -> MapResponse:
    """Discover URLs on a domain (sitemap + lightweight anchor crawl)."""
    logger.info("POST /map url=%s max=%s", req.url, req.max_pages)
    try:
        urls = await mapper.map_site(req)
    except Exception as exc:
        logger.exception("map failed for %s", req.url)
        return MapResponse(
            success=False, total_urls=0, urls=[], error=str(exc)
        )
    return MapResponse(success=True, total_urls=len(urls), urls=urls)


@app.get("/health", response_model=HealthResponse)
async def health_endpoint() -> HealthResponse:
    """Liveness/readiness probe."""
    return HealthResponse(
        status="ok",
        version="1.0.0",
        uptime_seconds=round(time.monotonic() - _started_at, 2),
        browser_ready=browser_manager.is_ready,
        cache_size=await cache.size(),
        active_jobs=await crawler.active_count(),
    )


# --------------------------------------------------------------------------- #
# Allow ``python main.py`` for convenience.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=False,
    )
