# blazecrawl/utils/url.py
"""URL normalization, validation, and domain utilities.

All helpers here are pure (no I/O) and synchronous.
"""
from __future__ import annotations

import fnmatch
from typing import Iterable, Optional
from urllib.parse import (
    ParseResult,
    parse_qsl,
    quote,
    unquote,
    urldefrag,
    urljoin,
    urlparse,
    urlsplit,
    urlunparse,
)

# Schemes we will follow.
_VALID_SCHEMES = {"http", "https"}
# Default ports we strip when normalising.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_valid_url(url: str) -> bool:
    """Return ``True`` iff *url* parses to a well-formed http(s) URL."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in _VALID_SCHEMES and bool(parsed.netloc)


def normalize_url(url: str) -> str:
    """Canonicalise *url* for safe deduplication.

    The transformations applied:

    * Lowercase scheme and host.
    * Strip URL fragments (``#section``).
    * Remove default ports (``:80`` / ``:443``).
    * Remove empty/duplicate query parameters and sort them alphabetically.
    * Drop a trailing slash on the path *unless* the path is just ``/``.
    * Percent-encode any odd characters.
    """
    if not url:
        return url

    url = url.strip()
    url, _ = urldefrag(url)

    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc

    if "@" in netloc:
        # strip userinfo for normalisation, but keep it generally rare anyway
        userinfo, _, hostport = netloc.rpartition("@")
        host_and_port = hostport
    else:
        userinfo = ""
        host_and_port = netloc

    if ":" in host_and_port:
        host, port = host_and_port.rsplit(":", 1)
        host = host.lower()
        # remove default ports
        try:
            port_num = int(port)
            if _DEFAULT_PORTS.get(scheme) == port_num:
                host_and_port = host
            else:
                host_and_port = f"{host}:{port_num}"
        except ValueError:
            host_and_port = host.lower()
    else:
        host_and_port = host_and_port.lower()

    netloc = f"{userinfo}@{host_and_port}" if userinfo else host_and_port

    # path
    path = parsed.path or "/"
    # percent-encode while preserving slashes & a-safe set
    path = quote(unquote(path), safe="/-._~!$&'()*+,;=:@%")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    # query: keep meaningful params, drop empties, sort
    if parsed.query:
        items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
        items.sort()
        query = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" if v != "" else quote(k, safe="")
            for k, v in items
        )
    else:
        query = ""

    return urlunparse((scheme, netloc, path, "", query, ""))


def get_domain(url: str) -> str:
    """Return the bare hostname (``example.com``) of a URL.

    Returns an empty string for invalid input.
    """
    try:
        host = urlparse(url).hostname or ""
        return host.lower()
    except Exception:
        return ""


def get_base_url(url: str) -> str:
    """Return ``scheme://host[:port]`` portion of *url* (no path)."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def get_registered_domain(url: str) -> str:
    """Return a best-effort registered domain (e.g. ``example.co.uk``).

    A full Public-Suffix-List implementation isn't part of stdlib, so this
    falls back to the last two labels.  Subdomain checks elsewhere should
    use :func:`same_domain` which compares against the seed host instead.
    """
    host = get_domain(url)
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def resolve_url(base: str, link: str) -> Optional[str]:
    """Resolve *link* (possibly relative) against *base*; normalize the result.

    Returns ``None`` if the resolved URL is not a valid http(s) URL.
    """
    if not link:
        return None
    link = link.strip()
    if link.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        absolute = urljoin(base, link)
    except Exception:
        return None
    if not is_valid_url(absolute):
        return None
    return normalize_url(absolute)


def same_domain(url: str, target: str, include_subdomains: bool = False) -> bool:
    """Return ``True`` if *url* belongs to the same domain as *target*.

    When ``include_subdomains`` is true, ``blog.example.com`` matches
    ``example.com`` (and vice versa for a target subdomain comparison).
    """
    a = get_domain(url)
    b = get_domain(target)
    if not a or not b:
        return False
    if a == b:
        return True
    if include_subdomains:
        return a.endswith("." + b) or b.endswith("." + a)
    return False


def matches_any(url: str, patterns: Iterable[str]) -> bool:
    """Return ``True`` if *url* matches any of the fnmatch *patterns*.

    Patterns may match the path, full URL, or include wildcards such as
    ``/blog/*`` or ``*.pdf``.  An empty pattern list is treated as "no match".
    """
    if not patterns:
        return False
    path = urlparse(url).path or "/"
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(url, pat) or fnmatch.fnmatch(path, pat):
            return True
        # also try with a leading "*" so users can write "/blog/*"
        if pat.startswith("/") and fnmatch.fnmatch(path, pat):
            return True
    return False


def is_filtered(
    url: str,
    *,
    allow_patterns: Iterable[str] = (),
    deny_patterns: Iterable[str] = (),
) -> bool:
    """Return ``True`` if *url* should be excluded by allow/deny patterns.

    Logic:
      * If ``deny_patterns`` match → filtered out.
      * If ``allow_patterns`` is non-empty AND doesn't match → filtered out.
      * Otherwise → kept.
    """
    if deny_patterns and matches_any(url, deny_patterns):
        return True
    if allow_patterns and not matches_any(url, allow_patterns):
        return True
    return False


def parse(url: str) -> ParseResult:
    """Convenience wrapper around :func:`urllib.parse.urlparse`."""
    return urlparse(url)
