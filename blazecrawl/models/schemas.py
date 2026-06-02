# blazecrawl/models/schemas.py
"""Pydantic v2 request & response schemas for the BlazeCrawl API."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class OutputFormat(str, Enum):
    """Supported response output formats."""

    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"
    RAW_HTML = "raw_html"


class JobStatus(str, Enum):
    """Crawl job lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------- #
# Shared models
# --------------------------------------------------------------------------- #
class Metadata(BaseModel):
    """Metadata extracted from a page."""

    model_config = ConfigDict(extra="allow")

    title: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    canonical: Optional[str] = None
    favicon: Optional[str] = None

    og_title: Optional[str] = None
    og_description: Optional[str] = None
    og_image: Optional[str] = None
    og_type: Optional[str] = None
    og_site_name: Optional[str] = None

    twitter_title: Optional[str] = None
    twitter_description: Optional[str] = None
    twitter_image: Optional[str] = None
    twitter_card: Optional[str] = None

    json_ld: List[Dict[str, Any]] = Field(default_factory=list)

    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    word_count: Optional[int] = None
    content_hash: Optional[str] = None


class PageData(BaseModel):
    """A single scraped page's payload."""

    url: str
    markdown: Optional[str] = None
    html: Optional[str] = None
    text: Optional[str] = None
    raw_html: Optional[str] = None
    metadata: Optional[Metadata] = None
    structured: Optional[Dict[str, Any]] = None
    links: Optional[List[str]] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# /scrape
# --------------------------------------------------------------------------- #
class ScrapeRequest(BaseModel):
    """Request body for ``POST /scrape``."""

    url: HttpUrl
    formats: List[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.MARKDOWN]
    )
    only_main_content: bool = True
    include_metadata: bool = True
    include_structured: bool = False
    include_links: bool = False
    wait_for_js: bool = False
    auto_browser_fallback: bool = True
    timeout: int = Field(default=30_000, ge=1_000, le=120_000)
    headers: Dict[str, str] = Field(default_factory=dict)
    remove_selectors: List[str] = Field(default_factory=list)
    bypass_cache: bool = False
    respect_robots_txt: bool = True
    wait_for_selector: Optional[str] = None

    @field_validator("formats", mode="before")
    @classmethod
    def _ensure_format_list(cls, v):  # type: ignore[no-untyped-def]
        if v is None:
            return [OutputFormat.MARKDOWN]
        return v


class ScrapeResponse(BaseModel):
    """Response body for ``POST /scrape``."""

    success: bool
    data: Optional[PageData] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# /crawl
# --------------------------------------------------------------------------- #
class CrawlRequest(BaseModel):
    """Request body for ``POST /crawl``."""

    url: HttpUrl
    max_depth: int = Field(default=3, ge=0, le=10)
    max_pages: int = Field(default=100, ge=1, le=10_000)
    formats: List[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.MARKDOWN]
    )
    only_main_content: bool = True
    include_metadata: bool = True
    include_structured: bool = False
    allow_patterns: List[str] = Field(default_factory=list)
    deny_patterns: List[str] = Field(default_factory=list)
    wait_for_js: bool = False
    respect_robots_txt: bool = True
    include_subdomains: bool = False
    concurrency: Optional[int] = Field(default=None, ge=1, le=50)
    headers: Dict[str, str] = Field(default_factory=dict)
    remove_selectors: List[str] = Field(default_factory=list)


class CrawlStartResponse(BaseModel):
    """Immediate response when a crawl is enqueued."""

    success: bool
    job_id: str
    status: JobStatus


class CrawlJobResponse(BaseModel):
    """Response body for ``GET /crawl/{job_id}``."""

    job_id: str
    status: JobStatus
    total_pages: int = 0
    pages_crawled: int = 0
    pages_failed: int = 0
    seed_url: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    data: List[PageData] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# /map
# --------------------------------------------------------------------------- #
class MapRequest(BaseModel):
    """Request body for ``POST /map``."""

    url: HttpUrl
    max_pages: int = Field(default=500, ge=1, le=10_000)
    include_subdomains: bool = False
    include_sitemap: bool = True
    search: Optional[str] = None
    respect_robots_txt: bool = True


class MapResponse(BaseModel):
    """Response body for ``POST /map``."""

    success: bool
    total_urls: int
    urls: List[str] = Field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    """Response body for ``GET /health``."""

    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0
    browser_ready: bool = False
    cache_size: int = 0
    active_jobs: int = 0


# --------------------------------------------------------------------------- #
# Generic error envelope
# --------------------------------------------------------------------------- #
class ErrorResponse(BaseModel):
    """Generic API error response envelope."""

    success: bool = False
    error: str
    detail: Optional[str] = None
