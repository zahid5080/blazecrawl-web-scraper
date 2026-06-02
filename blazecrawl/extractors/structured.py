# blazecrawl/extractors/structured.py
"""Extract structured data from HTML: tables, lists, and JSON-LD blocks."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def _extract_table(table: Tag) -> Dict[str, Any]:
    """Convert a ``<table>`` element to ``{headers: [...], rows: [[...], ...]}``."""
    headers: List[str] = []
    rows: List[List[str]] = []

    # try thead first
    thead = table.find("thead")
    if thead and isinstance(thead, Tag):
        for tr in thead.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                headers = cells
                break

    # fall back: first row whose cells are <th>
    if not headers:
        first_tr = table.find("tr")
        if first_tr and isinstance(first_tr, Tag):
            ths = first_tr.find_all("th")
            if ths:
                headers = [t.get_text(strip=True) for t in ths]

    # body rows
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        if tr.find("th") and not tr.find("td"):
            continue  # skip pure header rows
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)

    # dedupe: if the first row equals the headers, drop it
    if headers and rows and rows[0] == headers:
        rows = rows[1:]

    return {"headers": headers, "rows": rows}


def extract_tables(html: str) -> List[Dict[str, Any]]:
    """Return a list of tables extracted from *html*."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        try:
            data = _extract_table(table)
            if data.get("rows") or data.get("headers"):
                out.append(data)
        except Exception as exc:
            logger.debug("failed to extract table: %s", exc)
    return out


# --------------------------------------------------------------------------- #
# Lists
# --------------------------------------------------------------------------- #
def _list_items(list_tag: Tag) -> List[str]:
    items: List[str] = []
    for li in list_tag.find_all("li", recursive=False):
        text = li.get_text(separator=" ", strip=True)
        if text:
            items.append(text)
    return items


def extract_lists(html: str, max_items_per_list: int = 200) -> List[Dict[str, Any]]:
    """Return a list of ``{type, items}`` dicts for ``<ul>`` and ``<ol>`` tags."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, Any]] = []
    for list_tag in soup.find_all(["ul", "ol"]):
        # skip nested lists — they'll be captured as items of their parent
        if list_tag.find_parent(["ul", "ol"]):
            continue
        items = _list_items(list_tag)
        if not items:
            continue
        out.append(
            {
                "type": list_tag.name,
                "items": items[:max_items_per_list],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# JSON-LD
# --------------------------------------------------------------------------- #
def extract_json_ld(html: str) -> List[Dict[str, Any]]:
    """Return parsed JSON-LD blocks from ``<script type="application/ld+json">``."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = (script.string or script.get_text() or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(item)
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict):
                        out.append(item)
            else:
                out.append(data)
    return out


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
def extract_structured(html: Optional[str]) -> Dict[str, Any]:
    """Return a single dict bundling tables, lists, and JSON-LD."""
    if not html:
        return {"tables": [], "lists": [], "json_ld": []}
    return {
        "tables": extract_tables(html),
        "lists": extract_lists(html),
        "json_ld": extract_json_ld(html),
    }


# --------------------------------------------------------------------------- #
# Link extraction (used by crawler/mapper)
# --------------------------------------------------------------------------- #
def extract_links(html: str) -> List[str]:
    """Return all raw ``<a href>`` values from *html* (un-resolved)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    hrefs: List[str] = []
    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        href = a.get("href")
        if isinstance(href, str) and href.strip():
            hrefs.append(href.strip())
    return hrefs
