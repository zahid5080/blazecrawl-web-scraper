# blazecrawl/extractors/markdown.py
"""HTML → Markdown conversion with cleanup, link resolution, and image filtering."""
from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as _md

from utils.url import resolve_url

logger = logging.getLogger(__name__)

# Collapse 3+ blank lines to exactly 2.
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
# Collapse runs of trailing horizontal whitespace.
_RE_TRAILING_WS = re.compile(r"[ \t]+\n")
# Collapse runs of spaces (>1) within lines, preserving leading indent.
_RE_INNER_SPACES = re.compile(r"(?<=\S) {2,}(?=\S)")


def _absolutize_links(html: str, base_url: str) -> str:
    """Rewrite ``<a href>`` and ``<img src>`` to absolute URLs.

    Also drops ``data:`` image URIs entirely (they bloat output).
    """
    if not html or not base_url:
        return html or ""

    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        href = a.get("href")
        if isinstance(href, str):
            resolved = resolve_url(base_url, href)
            if resolved:
                a["href"] = resolved
            else:
                # unwrap anchors that resolve to garbage
                a.unwrap()

    for img in list(soup.find_all("img")):
        if not isinstance(img, Tag):
            continue
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if isinstance(src, str) and src.startswith("data:"):
            img.decompose()
            continue
        if isinstance(src, str):
            resolved = resolve_url(base_url, src)
            if resolved:
                img["src"] = resolved
            else:
                img.decompose()

    return str(soup)


def _clean_markdown(md: str) -> str:
    """Normalise whitespace in *md*."""
    if not md:
        return ""
    # Normalise CRLF.
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing whitespace on every line.
    md = _RE_TRAILING_WS.sub("\n", md)
    # Collapse internal multi-space.
    md = _RE_INNER_SPACES.sub(" ", md)
    # Collapse 3+ newlines to 2.
    md = _RE_MULTI_NEWLINE.sub("\n\n", md)
    return md.strip() + "\n"


def html_to_markdown(html: str, *, base_url: Optional[str] = None) -> str:
    """Convert *html* to clean Markdown.

    Preserves headings, bold, italic, links, images, tables, code blocks,
    and lists.  Relative URLs are resolved against *base_url* when provided.
    """
    if not html:
        return ""

    if base_url:
        html = _absolutize_links(html, base_url)

    try:
        md = _md(
            html,
            heading_style="ATX",        # use # headings
            bullets="-",                # use - for bullets
            strip=["script", "style", "noscript"],
            code_language="",
            wrap=False,
            escape_asterisks=False,
            escape_underscores=False,
        )
    except Exception as exc:
        logger.warning("markdownify failed: %s", exc)
        # last-ditch: strip tags
        md = BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)

    return _clean_markdown(md)
