# blazecrawl/core/scraper.py
"""Single-page scraping orchestrator.

Workflow per :meth:`Scraper.scrape`:

1. Validate & normalise the URL.
2. Check cache (unless the caller passes ``bypass_cache``).
3. Check robots.txt (unless disabled).
4. Acquire a per-domain rate-limit token.
5. Fetch the page (httpx for static, Playwright for JS rendering).
6. Run the extractors → main HTML, markdown, plain text, metadata, structured.
7. Build the :class:`PageData` model and (optionally) cache the result.
"""
from __future__ import annotations

import gzip
import logging
import re
import time
import zlib
from typing import Any, Dict, List, Optional

import httpx

from config import settings
from core.browser import browser_manager
from extractors.content import extract_main_content
from extractors.markdown import html_to_markdown
from extractors.metadata import extract_metadata
from extractors.structured import extract_links, extract_structured
from models.schemas import OutputFormat, PageData, ScrapeRequest
from services.cache import cache
from services.proxy import proxy_manager
from services.rate_limiter import rate_limiter
from services.robots import robots_checker
from utils.helpers import (
    async_retry,
    content_hash,
    next_user_agent,
    word_count,
)
from utils.url import is_valid_url, normalize_url, resolve_url

logger = logging.getLogger(__name__)


# HTTP status codes that strongly suggest anti-bot blocking rather than a
# genuine application error. We auto-retry with the headless browser when
# we see one of these (and `auto_browser_fallback` is enabled).
_ANTI_BOT_STATUS_CODES: frozenset[int] = frozenset({
    401,  # Unauthorized — often returned by WAFs before any auth challenge
    403,  # Forbidden — Cloudflare BM, Akamai, generic WAF blocks
    406,  # Not Acceptable — Imperva sometimes uses this for bots
    429,  # Too Many Requests — rate limit OR challenge
    503,  # Service Unavailable — Cloudflare "checking your browser"
})


# httpx error class names that often indicate TLS-layer fingerprinting or
# connection-level anti-bot behaviour. Matching by name (rather than `except
# httpx.RemoteProtocolError as exc`) keeps this resilient to httpx upgrades.
_ANTI_BOT_ERROR_NAMES: frozenset[str] = frozenset({
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "ProtocolError",
    "ReadTimeout",
    "ConnectTimeout",
})


class ScrapeError(Exception):
    """Raised when a scrape cannot be completed."""


class Scraper:
    """Stateless single-page scraper."""

    def __init__(self) -> None:
        self._max_bytes = settings.MAX_RESPONSE_BYTES

    # ------------------------------------------------------------------ API
    async def scrape(self, req: ScrapeRequest) -> PageData:
        """Scrape a single URL per the *req* configuration."""
        url = str(req.url)
        if not is_valid_url(url):
            raise ScrapeError(f"invalid URL: {url}")
        normalised = normalize_url(url)

        cache_key = self._cache_key(normalised, req)
        if not req.bypass_cache:
            cached = await cache.get(cache_key)
            if cached is not None:
                logger.info("cache hit for %s", normalised)
                return cached  # type: ignore[no-any-return]

        # robots.txt
        if req.respect_robots_txt:
            try:
                allowed = await robots_checker.can_fetch(normalised)
            except Exception as exc:
                logger.debug("robots check error for %s: %s — allowing", normalised, exc)
                allowed = True
            if not allowed:
                raise ScrapeError(f"blocked by robots.txt: {normalised}")
            crawl_delay = await robots_checker.crawl_delay(normalised)
            if crawl_delay is not None and crawl_delay > 0:
                await rate_limiter.configure_domain(
                    normalised, rate=1.0 / max(crawl_delay, 0.001)
                )

        # rate-limit
        await rate_limiter.acquire(normalised)

        # fetch — with automatic browser fallback for anti-bot status codes
        start = time.perf_counter()
        used_browser = req.wait_for_js
        try:
            if req.wait_for_js:
                fetched = await self._fetch_with_browser(normalised, req)
            else:
                fetched = await self._fetch_with_httpx(normalised, req)
        except Exception as exc:
            # If httpx couldn't even establish a connection (TLS handshake,
            # connection reset, etc.), some sites that fingerprint clients
            # at the TLS layer (Cloudflare, etc.) will still let real browsers
            # through. Fall back to the browser if enabled.
            if (
                req.auto_browser_fallback
                and not req.wait_for_js
                and self._looks_like_anti_bot_error(exc)
            ):
                logger.info(
                    "httpx fetch raised %s for %s — retrying with browser",
                    type(exc).__name__, normalised,
                )
                try:
                    fetched = await self._fetch_with_browser(normalised, req)
                    used_browser = True
                except Exception as exc2:
                    logger.warning(
                        "browser fallback also failed for %s: %s",
                        normalised, exc2,
                    )
                    raise ScrapeError(f"fetch failed: {exc2}") from exc2
            else:
                logger.warning("fetch failed for %s: %s", normalised, exc)
                raise ScrapeError(f"fetch failed: {exc}") from exc

        # Auto-fallback to the browser on anti-bot HTTP status codes from httpx.
        # Many sites (Cloudflare, Akamai-protected shops, etc.) serve a 403 or
        # 503 to plain HTTP clients but happily serve real browsers.
        if (
            not used_browser
            and req.auto_browser_fallback
            and fetched.get("status") in _ANTI_BOT_STATUS_CODES
        ):
            blocked_status = fetched.get("status")
            logger.info(
                "got HTTP %s from %s — retrying with headless browser",
                blocked_status, normalised,
            )
            try:
                fetched_browser = await self._fetch_with_browser(normalised, req)
            except Exception as exc:
                logger.warning(
                    "browser fallback failed for %s: %s — keeping httpx response",
                    normalised, exc,
                )
            else:
                # Only adopt the browser response if it actually got further
                # than httpx (i.e. didn't also return an anti-bot status).
                browser_status = fetched_browser.get("status")
                if (
                    browser_status is None
                    or browser_status not in _ANTI_BOT_STATUS_CODES
                ):
                    fetched = fetched_browser
                    used_browser = True
                else:
                    logger.info(
                        "browser also got HTTP %s — surfacing original error",
                        browser_status,
                    )

        elapsed_ms = (time.perf_counter() - start) * 1000.0

        raw_html: str = fetched.get("html") or ""
        status_code: Optional[int] = fetched.get("status")
        final_url: str = fetched.get("final_url") or normalised
        content_type: str = (fetched.get("content_type") or "").lower()

        # Fail fast on HTTP error responses — the body is almost never useful
        # content (it's an error page, anti-bot challenge, or binary blob),
        # and forwarding garbage to the extractors produces nonsense output.
        if status_code is not None and status_code >= 400:
            hint = ""
            if status_code in _ANTI_BOT_STATUS_CODES:
                if not req.auto_browser_fallback:
                    hint = (
                        " — the site is blocking automated requests. "
                        'Try setting "auto_browser_fallback": true (the default) '
                        'or "wait_for_js": true to use the headless browser.'
                    )
                elif used_browser:
                    hint = (
                        " — the site is blocking both the HTTP client AND the "
                        "headless browser. This usually means commercial bot "
                        "protection (Cloudflare Turnstile, DataDome, PerimeterX). "
                        "Configure a residential proxy via BLAZECRAWL_PROXIES."
                    )
                else:
                    hint = (
                        " — the headless browser fallback also failed. "
                        "Consider configuring a proxy via BLAZECRAWL_PROXIES."
                    )
            raise ScrapeError(
                f"HTTP {status_code} from {final_url}{hint}"
            )

        # Reject non-text responses up front. We only know how to extract
        # content from HTML / XHTML / XML / plain text.
        if content_type and not any(
            t in content_type
            for t in ("text/html", "application/xhtml", "application/xml", "text/xml", "text/plain")
        ):
            raise ScrapeError(
                f"unsupported Content-Type {content_type!r} from {final_url} "
                f"(BlazeCrawl only extracts HTML/XML/text responses)"
            )

        if not raw_html:
            raise ScrapeError(f"empty response body from {final_url}")

        # Cheap sanity check: if the body is mostly unprintable bytes (e.g.
        # the response was an encrypted anti-bot challenge that slipped past
        # the status check, or a binary type without a Content-Type header),
        # don't waste cycles running it through the extractor pipeline.
        sample = raw_html[:2048]
        if sample:
            replacement_ratio = sample.count("\ufffd") / len(sample)
            if replacement_ratio > 0.1:
                raise ScrapeError(
                    f"response body from {final_url} appears to be binary or "
                    f"corrupted (>{int(replacement_ratio * 100)}% undecodable "
                    f"bytes) — site may be serving an anti-bot challenge; try "
                    f'"wait_for_js": true'
                )

        # extract main content
        main_html, main_text = extract_main_content(
            raw_html,
            url=final_url,
            only_main_content=req.only_main_content,
            remove_selectors=req.remove_selectors,
        )

        # convert
        markdown_text: Optional[str] = None
        if OutputFormat.MARKDOWN in req.formats:
            markdown_text = html_to_markdown(main_html, base_url=final_url)

        page_text: Optional[str] = None
        if OutputFormat.TEXT in req.formats:
            page_text = main_text

        html_out: Optional[str] = None
        if OutputFormat.HTML in req.formats:
            html_out = main_html

        raw_html_out: Optional[str] = None
        if OutputFormat.RAW_HTML in req.formats:
            raw_html_out = raw_html

        # metadata
        metadata = None
        if req.include_metadata:
            metadata = extract_metadata(
                raw_html,
                url=final_url,
                status_code=status_code,
                response_time_ms=round(elapsed_ms, 2),
                text_for_word_count=main_text,
                content_hash_value=content_hash(main_text or raw_html),
            )

        # structured data
        structured = None
        if req.include_structured:
            structured = extract_structured(main_html or raw_html)

        # links
        link_list: Optional[List[str]] = None
        if req.include_links:
            seen: set[str] = set()
            link_list = []
            for href in extract_links(raw_html):
                resolved = resolve_url(final_url, href)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    link_list.append(resolved)

        data = PageData(
            url=final_url,
            markdown=markdown_text,
            html=html_out,
            text=page_text,
            raw_html=raw_html_out,
            metadata=metadata,
            structured=structured,
            links=link_list,
        )

        # cache
        try:
            await cache.set(cache_key, data)
        except Exception as exc:  # pragma: no cover
            logger.debug("cache set failed: %s", exc)

        return data

    # ----------------------------------------------------- fetch strategies
    @async_retry()
    async def _fetch_with_httpx(
        self,
        url: str,
        req: ScrapeRequest,
    ) -> Dict[str, Any]:
        """Fetch *url* with httpx (no JS rendering).

        Reads the raw (still-encoded) response body, then **manually**
        decompresses it based on the ``Content-Encoding`` header. We don't
        rely on httpx's automatic decompression because:

        * Some httpx versions don't decompress in streaming mode.
        * Some upstream servers (Cloudflare, etc.) send a Content-Encoding
          we never asked for in ``Accept-Encoding``.

        Charset is detected from (in order): the ``Content-Type`` header,
        any ``<meta charset>`` tag in the body, then standard fallbacks.
        """
        accept_encoding = "gzip, deflate"
        if self._has_brotli():
            accept_encoding = "gzip, deflate, br"

        # Pick a UA and the matching client-hint headers in lock-step. Sites
        # that compare the `User-Agent` against `sec-ch-ua` (and notice them
        # disagree about Chrome version / platform) will block. Our pool is
        # all desktop Chrome on Win/Mac so a single set of hints covers it.
        ua = next_user_agent()
        is_windows = "Windows" in ua
        is_mac = "Macintosh" in ua or "Mac OS X" in ua
        # Coarse Chrome major version sniff for sec-ch-ua. The default value
        # is fine for any modern Chrome — we just want it to *exist*.
        import re as _re
        m = _re.search(r"Chrome/(\d+)", ua)
        chrome_major = m.group(1) if m else "124"
        sec_ch_ua = (
            f'"Chromium";v="{chrome_major}", '
            f'"Not(A:Brand";v="24", '
            f'"Google Chrome";v="{chrome_major}"'
        )
        sec_ch_platform = (
            '"Windows"' if is_windows else '"macOS"' if is_mac else '"Linux"'
        )

        headers: Dict[str, str] = {
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": accept_encoding,
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            # Client hints — present in every Chrome request since v89.
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": sec_ch_platform,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        headers.update(req.headers or {})

        proxy_url = await proxy_manager.next_proxy()
        timeout = httpx.Timeout(
            max(req.timeout / 1000.0, 1.0),
            connect=10.0,
        )

        # Allow generously oversized *compressed* bodies (we'll enforce the
        # real cap after decompression). A 1 MB gzip can decode to 10 MB+.
        hard_byte_cap = max(self._max_bytes * 4, self._max_bytes + 1_000_000)

        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
            proxy=proxy_url,
            http2=False,
        ) as client:
            async with client.stream("GET", url) as resp:
                # Early bail if the server announces a colossal body.
                cl = resp.headers.get("content-length", "")
                if cl.isdigit() and int(cl) > hard_byte_cap:
                    raise ScrapeError(
                        f"response too large: Content-Length {cl} > {hard_byte_cap}"
                    )

                # Stream the *raw* (still-encoded) bytes with a hard cap.
                raw_chunks: List[bytes] = []
                total = 0
                async for chunk in resp.aiter_raw():
                    total += len(chunk)
                    if total > hard_byte_cap:
                        raise ScrapeError(
                            f"response too large (>{hard_byte_cap} encoded bytes)"
                        )
                    raw_chunks.append(chunk)
                raw_body = b"".join(raw_chunks)

                # Decompress based on Content-Encoding.
                content_encoding = (
                    resp.headers.get("content-encoding") or ""
                ).lower().strip()
                try:
                    body = self._decompress(raw_body, content_encoding)
                except ScrapeError:
                    raise
                except Exception as exc:
                    raise ScrapeError(
                        f"failed to decompress {content_encoding!r}-encoded "
                        f"response from {resp.url}: {exc}"
                    ) from exc

                if len(body) > self._max_bytes:
                    raise ScrapeError(
                        f"response too large (>{self._max_bytes} bytes after "
                        f"decompression)"
                    )

                content_type = resp.headers.get("content-type", "")
                text = self._decode(
                    body, content_type=content_type, hint=resp.encoding
                )
                return {
                    "html": text,
                    "status": resp.status_code,
                    "final_url": str(resp.url),
                    "content_type": content_type,
                }

    # ------------------------------------------------------- decompression
    # Module-level cache so we only probe for brotli once per process.
    _brotli_cached: Optional[bool] = None

    @staticmethod
    def _looks_like_anti_bot_error(exc: BaseException) -> bool:
        """Heuristic: does this httpx exception smell like anti-bot rejection?

        Sites that fingerprint clients at the TLS / connection layer
        (Cloudflare, Akamai) often kill the connection rather than send a
        proper HTTP response. We classify connection-level errors as
        "probably anti-bot, retry with browser" — but NOT errors that look
        like the URL is just plain bad (DNS, invalid port, etc.).
        """
        name = type(exc).__name__
        if name in _ANTI_BOT_ERROR_NAMES:
            return True
        # DNS / hostname errors should NOT trigger the browser retry —
        # the browser will fail the same way and we'd waste seconds.
        if "NameResolution" in name or "NameError" in name:
            return False
        # As a last resort, sniff the message text. httpx wraps lots of
        # low-level errors and the class name alone isn't always specific.
        msg = str(exc).lower()
        return any(
            tok in msg
            for tok in (
                "connection reset",
                "connection aborted",
                "peer closed connection",
                "ssl",
                "tls",
                "remote end closed",
            )
        )

    @classmethod
    def _has_brotli(cls) -> bool:
        """Return True iff the brotli (or brotlicffi) module is importable."""
        if cls._brotli_cached is None:
            try:
                import brotli  # noqa: F401
                cls._brotli_cached = True
            except ImportError:
                try:
                    import brotlicffi  # noqa: F401
                    cls._brotli_cached = True
                except ImportError:
                    cls._brotli_cached = False
        return cls._brotli_cached

    @staticmethod
    def _decompress(body: bytes, encoding: str) -> bytes:
        """Decompress *body* per a Content-Encoding header value.

        Handles ``gzip`` / ``x-gzip`` / ``deflate`` / ``br`` / ``zstd`` /
        ``identity``. Supports the (rare) comma-separated multi-encoding
        form per RFC 7231 §3.1.2.2.
        """
        if not body or not encoding or encoding == "identity":
            return body

        # Multi-encoding: decoders are applied in reverse of the listed order.
        encodings = [e.strip() for e in encoding.split(",") if e.strip()]
        for enc in reversed(encodings):
            if enc in ("gzip", "x-gzip"):
                body = gzip.decompress(body)
            elif enc == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    # Some servers send raw deflate without the zlib wrapper.
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            elif enc == "br":
                try:
                    import brotli  # type: ignore[import-not-found]
                except ImportError:
                    try:
                        import brotlicffi as brotli  # type: ignore
                    except ImportError:
                        raise ScrapeError(
                            "response is Brotli-encoded but neither `brotli` "
                            "nor `brotlicffi` is installed. "
                            "Run: pip install brotli"
                        )
                body = brotli.decompress(body)
            elif enc == "zstd":
                try:
                    import zstandard  # type: ignore[import-not-found]
                except ImportError:
                    raise ScrapeError(
                        "response is zstd-encoded but the `zstandard` package "
                        "is not installed. Run: pip install zstandard"
                    )
                body = zstandard.ZstdDecompressor().decompress(body)
            # Unknown encoding: leave the body unchanged and hope it's text.
        return body

    @staticmethod
    def _decode(body: bytes, content_type: str, hint: Optional[str]) -> str:
        """Decode *body* to text using the best available charset hints.

        Tries, in order: the encoding supplied by the response (parsed from
        ``Content-Type``), any ``<meta charset>`` tag found in the first
        4 KiB, then ``utf-8`` / ``windows-1252`` / ``iso-8859-1``. Falls
        back to lossy ``utf-8`` with replacement chars if nothing decodes
        cleanly.
        """
        encodings_to_try: List[str] = []
        if hint:
            encodings_to_try.append(hint)

        # Scan the first 4 KiB for an HTML meta charset declaration.
        head = body[:4096]
        m = re.search(
            rb'<meta[^>]+charset\s*=\s*["\']?([A-Za-z0-9_\-]+)',
            head,
            re.IGNORECASE,
        )
        if m:
            try:
                charset = m.group(1).decode("ascii").lower()
            except UnicodeDecodeError:
                charset = ""
            if charset and charset not in encodings_to_try:
                encodings_to_try.append(charset)

        for fallback in ("utf-8", "windows-1252", "iso-8859-1"):
            if fallback not in encodings_to_try:
                encodings_to_try.append(fallback)

        for enc in encodings_to_try:
            try:
                return body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue

        # Last resort: never raise, always return *something*.
        return body.decode("utf-8", errors="replace")

    async def _fetch_with_browser(
        self,
        url: str,
        req: ScrapeRequest,
    ) -> Dict[str, Any]:
        """Fetch *url* with Playwright (full JS rendering)."""
        return await browser_manager.render(
            url,
            timeout=req.timeout,
            wait_for_selector=req.wait_for_selector,
            extra_headers=req.headers or None,
        )

    # ------------------------------------------------------------- caching
    @staticmethod
    def _cache_key(url: str, req: ScrapeRequest) -> str:
        """Build a deterministic cache key from URL + cache-relevant flags."""
        fmts = ",".join(sorted(f.value for f in req.formats))
        return (
            f"scrape::{url}"
            f"::main={int(req.only_main_content)}"
            f"::meta={int(req.include_metadata)}"
            f"::structured={int(req.include_structured)}"
            f"::links={int(req.include_links)}"
            f"::js={int(req.wait_for_js)}"
            f"::fmt={fmts}"
            f"::rm={','.join(sorted(req.remove_selectors))}"
        )


# Process-global scraper.
scraper = Scraper()
