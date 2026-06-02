# blazecrawl/config.py
"""Central configuration & constants loaded from environment variables.

All settings can be overridden via environment variables prefixed with
``BLAZECRAWL_`` (e.g. ``BLAZECRAWL_PORT=9000``).
"""
from __future__ import annotations

import logging
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BLAZECRAWL_",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------- concurrency
    MAX_CONCURRENT_REQUESTS: int = 10
    CRAWLER_CONCURRENCY: int = 5

    # ---------------------------------------------------------------- timeouts
    REQUEST_TIMEOUT: float = 30.0  # seconds (httpx)
    BROWSER_TIMEOUT: int = 30_000  # ms (Playwright)
    NAVIGATION_TIMEOUT: int = 30_000  # ms (Playwright navigation)

    # ----------------------------------------------------------- rate limiting
    DEFAULT_RPS: float = 2.0  # requests per second per domain
    DEFAULT_BURST: int = 5
    POLITE_DELAY: float = 0.5  # seconds between same-domain crawler hits

    # ------------------------------------------------------------------ cache
    CACHE_TTL: int = 300  # 5 minutes
    CACHE_MAX_SIZE: int = 1_000

    # ------------------------------------------------------------------ limits
    MAX_RESPONSE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    MAX_CRAWL_PAGES: int = 1_000
    MAX_CRAWL_DEPTH: int = 10
    MAX_MAP_URLS: int = 5_000

    # ------------------------------------------------------------------ proxy
    PROXIES: List[str] = Field(default_factory=list)

    # ---------------------------------------------------------------- browser
    BROWSER_HEADLESS: bool = True
    BLOCK_RESOURCES: List[str] = Field(
        default_factory=lambda: ["image", "font", "media"]
    )
    AUTO_SCROLL: bool = True
    AUTO_SCROLL_STEP_PX: int = 600
    AUTO_SCROLL_MAX_STEPS: int = 25

    # ------------------------------------------------------------------ retry
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 1.0

    # ------------------------------------------------------------ user agents
    DEFAULT_USER_AGENT: str = (
        "Mozilla/5.0 (compatible; BlazeCrawl/1.0; "
        "+https://github.com/blazecrawl/blazecrawl)"
    )

    # ----------------------------------------------------------------- robots
    RESPECT_ROBOTS_BY_DEFAULT: bool = True

    # ------------------------------------------------------------ validators
    @field_validator("PROXIES", mode="before")
    @classmethod
    def _parse_proxies(cls, v):  # type: ignore[no-untyped-def]
        """Allow comma-separated env var to populate PROXIES list."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("BLOCK_RESOURCES", mode="before")
    @classmethod
    def _parse_block_resources(cls, v):  # type: ignore[no-untyped-def]
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


# Realistic user agents used for rotation (utils/helpers.py).
USER_AGENTS: List[str] = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


settings = Settings()


def configure_logging() -> None:
    """Configure the root logger with a sane format and the configured level."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # quiet some chatty libs
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
