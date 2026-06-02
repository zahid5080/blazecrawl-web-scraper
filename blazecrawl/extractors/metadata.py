# blazecrawl/extractors/metadata.py
"""Extract metadata from an HTML page: title, description, OG/Twitter, JSON-LD."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

from models.schemas import Metadata
from utils.helpers import word_count
from utils.url import resolve_url

logger = logging.getLogger(__name__)


def _meta_content(soup: BeautifulSoup, **attrs: str) -> Optional[str]:
    """Look up ``<meta>`` tag by attributes and return its ``content``."""
    tag = soup.find("meta", attrs=attrs)
    if tag and isinstance(tag, Tag):
        v = tag.get("content")
        if isinstance(v, list):
            v = " ".join(v)
        return v.strip() if isinstance(v, str) and v.strip() else None
    return None


def _link_href(soup: BeautifulSoup, **attrs: str) -> Optional[str]:
    """Look up ``<link>`` tag by attributes and return its ``href``."""
    tag = soup.find("link", attrs=attrs)
    if tag and isinstance(tag, Tag):
        v = tag.get("href")
        if isinstance(v, list):
            v = v[0] if v else None
        return v.strip() if isinstance(v, str) and v.strip() else None
    return None


def _detect_language(soup: BeautifulSoup) -> Optional[str]:
    """Best-effort language detection from the ``<html lang="">`` attribute."""
    html_tag = soup.find("html")
    if html_tag and isinstance(html_tag, Tag):
        lang = html_tag.get("lang")
        if isinstance(lang, list):
            lang = lang[0] if lang else None
        if isinstance(lang, str) and lang.strip():
            return lang.strip()
    # fall back to <meta http-equiv="content-language">
    return _meta_content(soup, **{"http-equiv": "content-language"})


def _extract_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extract & parse all ``<script type="application/ld+json">`` blocks."""
    blocks: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # try to repair common issues (trailing commas, etc.) by ignoring
            logger.debug("could not parse a JSON-LD block; skipping")
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    blocks.append(item)
        elif isinstance(data, dict):
            # @graph is a common wrapper
            graph = data.get("@graph") if isinstance(data, dict) else None
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict):
                        blocks.append(item)
            else:
                blocks.append(data)
    return blocks


def _resolve_optional(base: str, href: Optional[str]) -> Optional[str]:
    """Return resolved absolute URL or ``None``."""
    if not href:
        return None
    return resolve_url(base, href) or href


def extract_metadata(
    html: str,
    *,
    url: str,
    status_code: Optional[int] = None,
    response_time_ms: Optional[float] = None,
    text_for_word_count: Optional[str] = None,
    content_hash_value: Optional[str] = None,
) -> Metadata:
    """Parse *html* and return a :class:`Metadata` model.

    ``url`` is the page URL — used to resolve relative ``og:image`` / favicon
    references to absolute URLs.
    """
    soup = BeautifulSoup(html or "", "lxml")

    # --- core ----------------------------------------------------------------
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    description = _meta_content(soup, attrs={"name": "description"}) or _meta_content(
        soup, name="description"
    )

    canonical = _link_href(soup, rel="canonical")
    canonical = _resolve_optional(url, canonical)

    favicon = (
        _link_href(soup, rel="icon")
        or _link_href(soup, rel="shortcut icon")
        or _link_href(soup, rel="apple-touch-icon")
    )
    favicon = _resolve_optional(url, favicon)

    # --- Open Graph ----------------------------------------------------------
    og_title = _meta_content(soup, property="og:title")
    og_description = _meta_content(soup, property="og:description")
    og_image = _resolve_optional(url, _meta_content(soup, property="og:image"))
    og_type = _meta_content(soup, property="og:type")
    og_site_name = _meta_content(soup, property="og:site_name")

    # --- Twitter -------------------------------------------------------------
    twitter_title = _meta_content(soup, name="twitter:title")
    twitter_description = _meta_content(soup, name="twitter:description")
    twitter_image = _resolve_optional(url, _meta_content(soup, name="twitter:image"))
    twitter_card = _meta_content(soup, name="twitter:card")

    # --- JSON-LD -------------------------------------------------------------
    json_ld = _extract_json_ld(soup)

    # --- language ------------------------------------------------------------
    language = _detect_language(soup)

    return Metadata(
        title=title,
        description=description,
        language=language,
        canonical=canonical,
        favicon=favicon,
        og_title=og_title,
        og_description=og_description,
        og_image=og_image,
        og_type=og_type,
        og_site_name=og_site_name,
        twitter_title=twitter_title,
        twitter_description=twitter_description,
        twitter_image=twitter_image,
        twitter_card=twitter_card,
        json_ld=json_ld,
        status_code=status_code,
        response_time_ms=response_time_ms,
        word_count=word_count(text_for_word_count) if text_for_word_count else None,
        content_hash=content_hash_value,
    )
