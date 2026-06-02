# blazecrawl/extractors/content.py
"""Main-content extraction (Mozilla Readability-style) and boilerplate removal."""
from __future__ import annotations

import logging
from typing import Iterable, Optional, Tuple

from bs4 import BeautifulSoup, Comment

try:
    # readability-lxml exposes the Document class as `readability.Document`
    from readability import Document  # type: ignore
except Exception:  # pragma: no cover - imported lazily by user code
    Document = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Tags whose contents are almost always noise.
_DEFAULT_REMOVE_TAGS: Tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
)

# Tags representing structural chrome we strip when only_main_content=True.
_BOILERPLATE_TAGS: Tuple[str, ...] = (
    "nav",
    "footer",
    "header",
    "aside",
    "form",
)

# Common ad/related selectors we strip aggressively.
_AD_SELECTORS: Tuple[str, ...] = (
    ".ad",
    ".ads",
    ".advert",
    ".advertisement",
    "[id*='google_ads']",
    "[id*='adsense']",
    "[class*='ad-']",
    "[class*='-ad']",
    "[class*='cookie']",
    "[class*='consent']",
    "[id*='cookie']",
    "[id*='consent']",
    "[role='banner']",
    "[role='navigation']",
)


def _strip_tags(soup: BeautifulSoup, tags: Iterable[str]) -> None:
    for tag_name in tags:
        for tag in soup.find_all(tag_name):
            tag.decompose()


def _strip_selectors(soup: BeautifulSoup, selectors: Iterable[str]) -> None:
    for sel in selectors:
        sel = sel.strip()
        if not sel:
            continue
        try:
            for tag in soup.select(sel):
                tag.decompose()
        except Exception as exc:
            logger.debug("invalid CSS selector %r: %s", sel, exc)


def _strip_comments(soup: BeautifulSoup) -> None:
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()


def clean_html(
    html: str,
    *,
    only_main_content: bool = True,
    remove_selectors: Optional[Iterable[str]] = None,
) -> str:
    """Return a cleaned HTML string suitable for downstream extraction.

    Operations:

    * Always strip ``<script>``/``<style>``/``<noscript>`` etc.
    * Strip ad/cookie-banner selectors.
    * If ``only_main_content`` is true, strip ``nav``/``footer``/``header``/``aside``/``form``.
    * Apply user-supplied ``remove_selectors``.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "lxml")
    _strip_comments(soup)
    _strip_tags(soup, _DEFAULT_REMOVE_TAGS)
    _strip_selectors(soup, _AD_SELECTORS)
    if only_main_content:
        _strip_tags(soup, _BOILERPLATE_TAGS)
    if remove_selectors:
        _strip_selectors(soup, remove_selectors)

    # Remove now-empty elements (single pass — good enough for most pages).
    for tag in list(soup.find_all()):
        if tag.name in {"br", "hr", "img", "input", "source"}:
            continue
        if not tag.get_text(strip=True) and not tag.find(["img", "video", "audio"]):
            tag.decompose()

    return str(soup)


def extract_main_content(
    html: str,
    *,
    url: str = "",
    only_main_content: bool = True,
    remove_selectors: Optional[Iterable[str]] = None,
) -> Tuple[str, str]:
    """Return ``(main_html, main_text)`` — the primary article content of *html*.

    When ``only_main_content`` is true we try Readability first; if it
    fails or produces nothing useful we fall back to the cleaned full body.
    When false we always return the cleaned full body.
    """
    if not html:
        return "", ""

    cleaned_full = clean_html(
        html,
        only_main_content=only_main_content,
        remove_selectors=remove_selectors,
    )

    if not only_main_content:
        soup = BeautifulSoup(cleaned_full, "lxml")
        body = soup.find("body") or soup
        return str(body), body.get_text(separator="\n", strip=True)

    # Try readability.
    main_html: Optional[str] = None
    if Document is not None:
        try:
            doc = Document(cleaned_full, url=url or None)
            main_html = doc.summary(html_partial=True)
        except Exception as exc:
            logger.debug("readability failed for %s: %s", url, exc)
            main_html = None

    if not main_html or len(BeautifulSoup(main_html, "lxml").get_text(strip=True)) < 50:
        # Fallback: prefer <main>, <article>, then <body>
        soup = BeautifulSoup(cleaned_full, "lxml")
        candidate = (
            soup.find("main")
            or soup.find("article")
            or soup.find("body")
            or soup
        )
        main_html = str(candidate)

    # Final sweep of leftover ads/cookie banners inside the main content too.
    main_soup = BeautifulSoup(main_html, "lxml")
    _strip_selectors(main_soup, _AD_SELECTORS)
    if remove_selectors:
        _strip_selectors(main_soup, remove_selectors)

    main_html_final = str(main_soup)
    main_text = main_soup.get_text(separator="\n", strip=True)
    return main_html_final, main_text
