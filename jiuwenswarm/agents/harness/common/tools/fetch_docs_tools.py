# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Technical documentation fetcher, Markdown extractor, and local cache tool."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from typing import Any
from urllib.parse import ParseResult, urldefrag, urljoin, urlparse, urlunparse

import httpx
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.rails.permissions.network_scope import (
    normalize_network_host,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import (
    unsafe_public_https_url_reason,
)
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_ssl_verify
from jiuwenswarm.common import utils as common_utils

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 1
_MIN_MAX_DEPTH = 0
_MAX_MAX_DEPTH = 3

DEFAULT_MAX_PAGES = 10
_MIN_MAX_PAGES = 1
_MAX_MAX_PAGES = 30

DEFAULT_TIMEOUT_SECONDS = 15
_MIN_TIMEOUT_SECONDS = 5
_MAX_TIMEOUT_SECONDS = 60

DEFAULT_MAX_CHARS = 30_000
_MIN_MAX_CHARS = 1_000
_MAX_CHARS_CEILING = 100_000

DEFAULT_TTL_SECONDS = 3_600
_MIN_TTL_SECONDS = 0
_MAX_TTL_SECONDS = 604_800  # 7 days

MAX_BYTES_PER_PAGE = 1_048_576  # 1 MiB
_MAX_REDIRECT_HOPS = 5
_INDEX_VERSION = 1

_NAT64_NETWORK = ipaddress.IPv6Network("64:ff9b::/96")
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/markdown",
        "text/x-markdown",
        "text/plain",
    }
)
_MARKDOWN_OR_TEXT_EXTS = (".md", ".mdx", ".txt")
_HTML_OR_EMPTY_ALLOWED_EXTS = (".html", ".htm", ".xhtml", ".md", ".mdx", ".txt")
_SKIP_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".bmp",
        ".css",
        ".js",
        ".mjs",
        ".cjs",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".mp4",
        ".webm",
        ".mp3",
        ".wav",
        ".wasm",
        ".json",
        ".xml",
        ".rss",
        ".atom",
    }
)
_STRIP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "nav",
        "footer",
        "aside",
        "noscript",
        "svg",
        "template",
        "iframe",
    }
)
_NO_LINK_TAGS = frozenset(
    {
        "script",
        "style",
        "footer",
        "noscript",
        "svg",
        "template",
    }
)
_BLOCK_BREAK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
    }
)
_CHARSET_HEADER_RE = re.compile(r"charset=([^\s;]+)", flags=re.IGNORECASE)
_CHARSET_META_RE = re.compile(
    br"""<meta[^>]+charset=["']?\s*([A-Za-z0-9._-]+)""",
    flags=re.IGNORECASE,
)
_CODE_LANG_RE = re.compile(
    r"(?:^|\s)(?:language-|lang-|highlight-source-)([A-Za-z0-9_+-]+)"
)
_MD_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+")

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JiuwenSwarm-fetch_docs/1.0; "
        "+https://github.com/openJiuwen-ai/jiuwenswarm)"
    ),
    "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain;q=0.9,*/*;q=0.1",
}


@dataclass(frozen=True)
class FetchDocsRequest:
    """Normalized request parameters for ``fetch_docs``."""

    url: str
    max_depth: int
    max_pages: int
    timeout_seconds: int
    max_chars: int
    refresh: bool
    query: str | None
    ttl_seconds: int
    path_prefix: str | None


@dataclass(frozen=True)
class _FetchedPage:
    """Normalized extracted representation of one fetched documentation page."""

    url: str
    status_code: int
    content_type: str
    title: str
    markdown: str
    headings: tuple[str, ...]
    summary: str
    links: tuple[str, ...]
    depth: int
    truncated_by_bytes: bool = False


@dataclass(frozen=True)
class _RawFetchedPage:
    """Raw decoded HTTP response before Markdown or HTML extraction."""

    url: str
    status_code: int
    content_type: str
    decoded_text: str
    truncated_by_bytes: bool


@dataclass(frozen=True)
class _CrawlResult:
    """Result of crawling documentation pages for one ``fetch_docs`` request."""

    req: FetchDocsRequest
    final_entry_url: str
    effective_prefix: str
    pages: list[_FetchedPage]
    errors: list[str]
    truncated_by_max_pages: bool = False


@dataclass
class _CrawlState:
    """Mutable BFS state shared across crawl helper steps."""

    req: FetchDocsRequest
    effective_prefix: str
    allowed_netloc: str
    final_entry_url: str
    fetched_pages: list[_FetchedPage]
    errors: list[str]
    visited: set[str]
    indexed_urls: set[str]
    queue: deque[tuple[str, int]]


def get_fetch_docs_cache_dir() -> Path:
    """Return the workspace-scoped cache directory for ``fetch_docs``."""
    return common_utils.get_agent_workspace_dir() / ".cache" / "fetch_docs"


def _clamp_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    """Parse an integer input safely and clamp it to ``[minimum, maximum]``."""
    if raw is None or isinstance(raw, bool):
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    return max(minimum, min(value, maximum))


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    """Coerce a boolean input cleanly."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return bool(raw)


def _strip_ip_literal(text: str) -> str:
    """Normalize an IP literal string by stripping brackets and zone IDs."""
    cleaned = str(text or "").strip().strip("[]")
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    return cleaned


def _is_ip_literal(host: str) -> bool:
    """Return True when ``host`` parses as an IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(_strip_ip_literal(host))
        return True
    except ValueError:
        return False


def _is_public_ip(ip_text: str) -> bool:
    """Return True only when ``ip_text`` is a publicly routable unicast address."""
    cleaned = _strip_ip_literal(ip_text)
    if not cleaned:
        return False
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
    ):
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None and not _is_public_ip(str(ip.ipv4_mapped)):
            return False
        if ip in _NAT64_NETWORK:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            return _is_public_ip(str(embedded))
    return True


def _validate_url_syntax(url: str) -> ParseResult:
    """Validate that ``url`` is a clean, non-internal public HTTPS URL."""
    text = str(url or "").strip()
    if not text:
        raise ValueError("url cannot be empty")
    if any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise ValueError("url must not contain whitespace or control characters")
    parsed = urlparse(text)
    reason = unsafe_public_https_url_reason(parsed)
    if reason is not None:
        raise ValueError(f"Unsafe URL rejected ({reason}): {text}")
    hostname = parsed.hostname or ""
    if _is_ip_literal(hostname) and not _is_public_ip(hostname):
        raise ValueError(f"Unsafe URL rejected (network_host_not_public): {text}")
    return parsed


def _validate_url_and_dns(url: str) -> ParseResult:
    """Validate URL syntax and confirm all resolved DNS targets are public IPs."""
    parsed = _validate_url_syntax(url)
    host = normalize_network_host(parsed.hostname)
    port = parsed.port or 443
    if _is_ip_literal(host):
        return parsed
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"DNS resolution failed for {host}: {exc}") from exc
    if not infos:
        raise ValueError(f"DNS resolution returned no addresses for {host}")
    for info in infos:
        sockaddr = info[4]
        addr_text = str(sockaddr[0]) if sockaddr else ""
        if not _is_public_ip(addr_text):
            raise ValueError(
                f"Unsafe URL rejected (network_host_not_public): "
                f"{host} resolved to {addr_text}"
            )
    return parsed


def _url_netloc(parsed: ParseResult) -> str:
    """Build a canonical lowercase ``host[:port]`` omitting default ``:443``."""
    host = normalize_network_host(parsed.hostname)
    if ":" in host and not host.startswith("["):
        host_repr = f"[{host}]"
    else:
        host_repr = host
    port = parsed.port
    scheme = parsed.scheme.lower()
    if port is not None and not (scheme == "https" and port == 443):
        return f"{host_repr}:{port}"
    return host_repr


def _canonicalize_url(url: str) -> str:
    """Canonicalize an HTTPS URL: drop fragment, normalize host/port/path."""
    defragged, _ = urldefrag(str(url or "").strip())
    parsed = urlparse(defragged)
    scheme = parsed.scheme.lower()
    netloc = _url_netloc(parsed)
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _derive_path_prefix(path: str) -> str:
    """Derive a default directory/section path prefix from an entry URL path."""
    cleaned = re.sub(r"/+", "/", path or "/")
    if cleaned in {"", "/"}:
        return "/"
    if cleaned.endswith("/"):
        return cleaned
    parent, _, last = cleaned.rpartition("/")
    if "." in last:
        return f"{parent}/" if parent else "/"
    return cleaned


def _normalize_path_prefix(raw_prefix: Any) -> str | None:
    """Validate and normalize an explicit ``path_prefix`` argument."""
    if raw_prefix is None:
        return None
    text = str(raw_prefix).strip()
    if not text:
        return None
    if "://" in text:
        raise ValueError("path_prefix must be a URL path prefix, not a full URL")
    if "?" in text or "#" in text or any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise ValueError(f"Invalid path_prefix: {text!r}")
    normalized = re.sub(r"/+", "/", "/" + text.lstrip("/"))
    return normalized


def _path_matches_prefix(candidate_path: str, prefix: str) -> bool:
    """Return True when ``candidate_path`` lies within ``prefix`` on segment boundaries."""
    path = re.sub(r"/+", "/", candidate_path or "/")
    if prefix == "/":
        return True
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(f"{prefix}/")


def _normalize_request(inputs: dict[str, Any]) -> FetchDocsRequest:
    """Normalize and validate raw ``fetch_docs`` inputs into a ``FetchDocsRequest``."""
    raw_url = str(inputs.get("url") or "").strip()
    parsed = _validate_url_syntax(raw_url)
    canonical_url = _canonicalize_url(raw_url)

    max_depth = _clamp_int(
        inputs.get("max_depth"), DEFAULT_MAX_DEPTH, _MIN_MAX_DEPTH, _MAX_MAX_DEPTH
    )
    max_pages = _clamp_int(
        inputs.get("max_pages"), DEFAULT_MAX_PAGES, _MIN_MAX_PAGES, _MAX_MAX_PAGES
    )
    timeout_seconds = _clamp_int(
        inputs.get("timeout_seconds"),
        DEFAULT_TIMEOUT_SECONDS,
        _MIN_TIMEOUT_SECONDS,
        _MAX_TIMEOUT_SECONDS,
    )
    max_chars = _clamp_int(
        inputs.get("max_chars"), DEFAULT_MAX_CHARS, _MIN_MAX_CHARS, _MAX_CHARS_CEILING
    )
    ttl_seconds = _clamp_int(
        inputs.get("ttl_seconds"), DEFAULT_TTL_SECONDS, _MIN_TTL_SECONDS, _MAX_TTL_SECONDS
    )
    refresh = _coerce_bool(inputs.get("refresh"), default=False)
    raw_query = inputs.get("query")
    query = str(raw_query).strip() or None if raw_query is not None else None

    path_prefix = _normalize_path_prefix(inputs.get("path_prefix"))
    if path_prefix is not None:
        entry_path = parsed.path or "/"
        if not _path_matches_prefix(entry_path, path_prefix):
            raise ValueError(
                f"Entry URL path {entry_path!r} does not match path_prefix {path_prefix!r}"
            )

    return FetchDocsRequest(
        url=canonical_url,
        max_depth=max_depth,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
        max_chars=max_chars,
        refresh=refresh,
        query=query,
        ttl_seconds=ttl_seconds,
        path_prefix=path_prefix,
    )


def _extract_mime_type(content_type: str) -> str:
    """Extract the lowercased media type from a ``Content-Type`` header."""
    return str(content_type or "").split(";", 1)[0].strip().lower()


def _extract_declared_charset(content_type: str, head_bytes: bytes) -> str:
    """Extract an explicitly declared charset from HTTP headers or HTML meta."""
    header_match = _CHARSET_HEADER_RE.search(content_type or "")
    if header_match:
        return header_match.group(1).strip().strip("\"'").lower()
    meta_match = _CHARSET_META_RE.search(head_bytes[:4096])
    if meta_match:
        return meta_match.group(1).decode("ascii", errors="ignore").strip().lower()
    return ""


def _decode_response_bytes(
    raw_bytes: bytes,
    content_type: str,
    *,
    truncated_by_bytes: bool = False,
) -> str:
    """Decode response bytes safely, handling partial trailing bytes on truncation."""
    if not raw_bytes:
        return ""
    declared = _extract_declared_charset(content_type, raw_bytes)
    candidates: list[str] = []
    if declared and declared not in {"iso-8859-1", "latin-1", "latin1"}:
        candidates.append(declared)
    candidates.extend(["utf-8", "gb18030", "cp1252", "iso-8859-1"])

    seen: set[str] = set()
    for enc in candidates:
        norm = enc.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        try:
            return raw_bytes.decode(norm, errors="strict")
        except UnicodeDecodeError as exc:
            if truncated_by_bytes and exc.end == len(raw_bytes) and exc.start >= len(raw_bytes) - 4:
                return raw_bytes.decode(norm, errors="replace")
            continue
        except LookupError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _extract_lang_from_attrs(attrs: list[tuple[str, str | None]]) -> str:
    """Extract a code block language hint from element ``class`` attributes."""
    for key, val in attrs:
        if key == "class" and val:
            match = _CODE_LANG_RE.search(val)
            if match:
                return match.group(1)
    return ""


def _fence_for_code(code_text: str) -> str:
    """Return a backtick fence longer than any backtick run in ``code_text``."""
    longest = 0
    for match in re.finditer(r"`+", code_text):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def _format_inline_code(text: str) -> str:
    """Wrap inline code cleanly in backticks."""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if "`" not in cleaned:
        return f"`{cleaned}`"
    fence = "`" * (max(len(m.group(0)) for m in re.finditer(r"`+", cleaned)) + 1)
    return f"{fence} {cleaned} {fence}"


def _is_collectible_href(href: str) -> bool:
    """Return True when an HTML href is a crawlable link candidate (not fragment/script)."""
    stripped = str(href or "").strip()
    if not stripped or stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    for scheme_prefix in ("javascript:", "mailto:", "tel:", "data:", "blob:"):
        if lowered.startswith(scheme_prefix):
            return False
    return True


def _step_code_fence(line: str, in_fence: bool, fence_marker: str) -> tuple[bool, str, bool]:
    """Update code-fence state for ``line`` and report whether ``line`` is a fence."""
    fence_match = _FENCE_LINE_RE.match(line)
    if not fence_match:
        return in_fence, fence_marker, False
    marker = fence_match.group(1)
    if not in_fence:
        return True, marker[0] * len(marker), True
    if line.lstrip().startswith(fence_marker):
        return False, "", True
    return in_fence, fence_marker, True


def _postprocess_markdown(raw_text: str) -> str:
    """Normalize blank lines and trailing whitespace without corrupting code fences."""
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cleaned_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    blank_run = 0

    for line in lines:
        stripped = line.rstrip()
        in_fence, fence_marker, is_fence = _step_code_fence(stripped, in_fence, fence_marker)
        if is_fence:
            cleaned_lines.append(stripped)
            blank_run = 0
            continue
        if in_fence:
            cleaned_lines.append(stripped)
            continue
        if not stripped:
            blank_run += 1
            if blank_run <= 1 and cleaned_lines:
                cleaned_lines.append("")
            continue
        blank_run = 0
        cleaned_lines.append(stripped)

    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


_START_SIMPLE_EMIT = {"strong": "**", "b": "**", "em": "*", "i": "*", "br": "\n"}
_END_SIMPLE_EMIT = {"strong": "**", "b": "**", "em": "*", "i": "*"}
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class _DocsHtmlToMarkdownParser(HTMLParser):
    """Stdlib HTML-to-Markdown converter retaining structure and nav link discovery."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []
        self._seen_links: set[str] = set()
        self.headings: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._strip_depth = 0
        self._no_link_depth = 0
        self._out: list[str] = []

        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._first_h1 = ""

        self._list_stack: list[tuple[str, int]] = []
        self._in_inline_code = False
        self._inline_code_parts: list[str] = []

        self._pre_depth = 0
        self._pre_lang = ""
        self._pre_parts: list[str] = []

        self._link_active = False
        self._link_href = ""
        self._link_parts: list[str] = []

        self._blockquote_stack: list[list[str]] = []
        self._in_table = False
        self._table_rows: list[tuple[bool, list[str]]] = []
        self._current_row_cells: list[str] = []
        self._current_row_is_header = False
        self._in_cell = False
        self._cell_parts: list[str] = []

    def _target_buffer(self) -> list[str]:
        if self._pre_depth > 0 or self._in_inline_code:
            return self._pre_parts if self._pre_depth > 0 else self._inline_code_parts
        if self._heading_level is not None or self._in_cell:
            return self._heading_parts if self._heading_level is not None else self._cell_parts
        if self._link_active:
            return self._link_parts
        if self._blockquote_stack:
            return self._blockquote_stack[-1]
        return self._out

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._target_buffer().append(text)

    def _buffer_ends_with(self, suffixes: tuple[str, ...]) -> bool:
        buf = self._target_buffer()
        if not buf:
            return True
        last = buf[-1]
        return last.endswith(suffixes)

    def _ensure_newline(self, count: int = 1) -> None:
        buf = self._target_buffer()
        if not buf:
            return
        tail = "".join(buf[-3:])
        existing = len(tail) - len(tail.rstrip("\n"))
        if existing < count:
            buf.append("\n" * (count - existing))

    def _record_link(self, href: str) -> str:
        if not _is_collectible_href(href):
            return ""
        resolved = urljoin(self.base_url, href.strip())
        defragged, _ = urldefrag(resolved)
        if defragged and defragged not in self._seen_links:
            self._seen_links.add(defragged)
            self.links.append(defragged)
        return defragged

    def _start_link(self, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        resolved = self._record_link(attr_map.get("href", ""))
        if self._strip_depth == 0 and not self._link_active:
            self._link_active = True
            self._link_href = resolved
            self._link_parts = []

    def _start_heading(self, lowered: str, _attrs: list[tuple[str, str | None]]) -> None:
        self._ensure_newline(2)
        self._heading_level = int(lowered[1])
        self._heading_parts = []

    def _start_pre(self, _lowered: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._pre_depth == 0:
            self._ensure_newline(2)
            self._pre_lang = _extract_lang_from_attrs(attrs)
            self._pre_parts = []
        self._pre_depth += 1

    def _start_code(self, _lowered: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._pre_depth > 0:
            if not self._pre_lang:
                self._pre_lang = _extract_lang_from_attrs(attrs)
        elif not self._in_inline_code:
            self._in_inline_code = True
            self._inline_code_parts = []

    def _start_blockquote(self, _lowered: str, _attrs: list[tuple[str, str | None]]) -> None:
        self._ensure_newline(2)
        self._blockquote_stack.append([])

    def _start_table_tag(self, lowered: str, _attrs: list[tuple[str, str | None]]) -> None:
        if lowered == "table":
            self._ensure_newline(2)
            self._in_table = True
            self._table_rows = []
            return
        if not self._in_table:
            return
        if lowered == "tr":
            self._current_row_cells = []
            self._current_row_is_header = False
            return
        if lowered in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []
            if lowered == "th":
                self._current_row_is_header = True

    def _start_list_tag(self, lowered: str, _attrs: list[tuple[str, str | None]]) -> None:
        if lowered in {"ul", "ol"}:
            self._ensure_newline(1 if self._list_stack else 2)
            self._list_stack.append((lowered, 0))
            return
        if lowered == "li" and self._list_stack:
            list_kind, count = self._list_stack[-1]
            count += 1
            self._list_stack[-1] = (list_kind, count)
            self._ensure_newline(1)
            indent = "  " * (len(self._list_stack) - 1)
            bullet = "- " if list_kind == "ul" else f"{count}. "
            self._emit(f"{indent}{bullet}")

    def _start_hr(self, _lowered: str, _attrs: list[tuple[str, str | None]]) -> None:
        self._ensure_newline(2)
        self._emit("---\n\n")

    _START_TAG_DISPATCH: dict[str, Callable[..., None]] = {
        "h1": _start_heading,
        "h2": _start_heading,
        "h3": _start_heading,
        "h4": _start_heading,
        "h5": _start_heading,
        "h6": _start_heading,
        "pre": _start_pre,
        "code": _start_code,
        "blockquote": _start_blockquote,
        "table": _start_table_tag,
        "tr": _start_table_tag,
        "th": _start_table_tag,
        "td": _start_table_tag,
        "ul": _start_list_tag,
        "ol": _start_list_tag,
        "li": _start_list_tag,
        "hr": _start_hr,
    }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "title" and self._strip_depth == 0:
            self._in_title = True
        if lowered == "a" and self._no_link_depth == 0:
            self._start_link(attrs)
        if lowered in _NO_LINK_TAGS:
            self._no_link_depth += 1
        if lowered in _STRIP_CONTENT_TAGS:
            self._strip_depth += 1
            return
        if self._strip_depth > 0:
            return
        handler = self._START_TAG_DISPATCH.get(lowered)
        if handler is not None:
            handler(self, lowered, attrs)
        elif lowered in _START_SIMPLE_EMIT:
            self._emit(_START_SIMPLE_EMIT[lowered])
        elif lowered in _BLOCK_BREAK_TAGS:
            self._ensure_newline(2)

    def _end_link(self, _lowered: str) -> None:
        if not self._link_active:
            return
        label = re.sub(r"\s+", " ", "".join(self._link_parts)).strip()
        href = self._link_href
        self._link_active = False
        self._link_href = ""
        self._link_parts = []
        if label and href:
            self._emit(f"[{label}]({href})")
        elif label:
            self._emit(label)

    def _end_heading(self, _lowered: str) -> None:
        if self._heading_level is None:
            return
        level = self._heading_level
        text = re.sub(r"\s+", " ", "".join(self._heading_parts)).strip()
        self._heading_level = None
        self._heading_parts = []
        if not text:
            return
        marker = "#" * level
        self._emit(f"{marker} {text}\n\n")
        self.headings.append(f"{marker} {text}")
        if level == 1 and not self._first_h1:
            self._first_h1 = text

    def _end_pre(self, _lowered: str) -> None:
        if self._pre_depth <= 0:
            return
        self._pre_depth -= 1
        if self._pre_depth != 0:
            return
        code_body = "".join(self._pre_parts).strip("\r\n")
        lang = self._pre_lang
        self._pre_lang = ""
        self._pre_parts = []
        fence = _fence_for_code(code_body)
        self._emit(f"{fence}{lang}\n{code_body}\n{fence}\n\n")

    def _end_inline_code(self, _lowered: str) -> None:
        if self._pre_depth != 0 or not self._in_inline_code:
            return
        code_text = re.sub(r"\s+", " ", "".join(self._inline_code_parts)).strip()
        self._in_inline_code = False
        self._inline_code_parts = []
        if code_text:
            self._emit(_format_inline_code(code_text))

    def _end_blockquote(self, _lowered: str) -> None:
        if not self._blockquote_stack:
            return
        raw_quote = _postprocess_markdown("".join(self._blockquote_stack.pop()))
        if not raw_quote:
            return
        quoted_lines: list[str] = []
        for line in raw_quote.splitlines():
            quoted_lines.append(f"> {line}" if line else ">")
        self._emit("\n".join(quoted_lines) + "\n\n")

    def _end_table_tag(self, lowered: str) -> None:
        if not self._in_table:
            return
        if lowered in {"th", "td"} and self._in_cell:
            cell_text = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
            self._current_row_cells.append(cell_text.replace("|", "\\|"))
            self._in_cell = False
            self._cell_parts = []
            return
        if lowered == "tr":
            if self._current_row_cells:
                self._table_rows.append((self._current_row_is_header, self._current_row_cells))
            self._current_row_cells = []
            self._current_row_is_header = False
            return
        if lowered == "table":
            self._in_table = False
            self._flush_table()

    def _end_list_tag(self, lowered: str) -> None:
        if lowered == "li":
            self._ensure_newline(1)
            return
        if lowered in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()
            self._ensure_newline(1 if self._list_stack else 2)

    _END_TAG_DISPATCH: dict[str, Callable[..., None]] = {
        "a": _end_link,
        "h1": _end_heading,
        "h2": _end_heading,
        "h3": _end_heading,
        "h4": _end_heading,
        "h5": _end_heading,
        "h6": _end_heading,
        "pre": _end_pre,
        "code": _end_inline_code,
        "blockquote": _end_blockquote,
        "th": _end_table_tag,
        "td": _end_table_tag,
        "tr": _end_table_tag,
        "table": _end_table_tag,
        "ul": _end_list_tag,
        "ol": _end_list_tag,
        "li": _end_list_tag,
    }

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        if lowered in _NO_LINK_TAGS and self._no_link_depth > 0:
            self._no_link_depth -= 1
        if lowered in _STRIP_CONTENT_TAGS and self._strip_depth > 0:
            self._strip_depth -= 1
            return
        if self._strip_depth > 0:
            return
        handler = self._END_TAG_DISPATCH.get(lowered)
        if handler is not None:
            handler(self, lowered)
        elif lowered in _END_SIMPLE_EMIT:
            self._emit(_END_SIMPLE_EMIT[lowered])
        elif lowered in _BLOCK_BREAK_TAGS:
            self._ensure_newline(2)

    def _flush_table(self) -> None:
        if not self._table_rows:
            return
        col_count = max(len(cells) for _, cells in self._table_rows)
        if col_count == 0:
            self._table_rows = []
            return
        rows = [cells + [""] * (col_count - len(cells)) for _, cells in self._table_rows]
        lines = ["| " + " | ".join(rows[0]) + " |"]
        lines.append("| " + " | ".join(["---"] * col_count) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        self._table_rows = []
        self._emit("\n".join(lines) + "\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and self._strip_depth == 0:
            self._title_parts.append(data)
            return
        if self._strip_depth > 0:
            return
        if self._pre_depth > 0:
            self._emit(data)
            return
        collapsed = re.sub(r"[ \t\r\n]+", " ", data)
        if not collapsed:
            return
        if self._buffer_ends_with(("\n", " ")):
            collapsed = collapsed.lstrip(" ")
        if collapsed:
            self._emit(collapsed)

    def get_title(self) -> str:
        raw = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
        return raw or self._first_h1

    def get_markdown(self) -> str:
        return _postprocess_markdown("".join(self._out))


def _is_passthrough_doc(url: str, mime: str) -> bool:
    """Return True when the fetched content is already Markdown or plain text."""
    if mime in {"text/markdown", "text/x-markdown"}:
        return True
    path_lower = urlparse(url).path.lower()
    if mime in {"text/plain", ""} and path_lower.endswith(_MARKDOWN_OR_TEXT_EXTS):
        return True
    return mime == "text/plain"


def _collect_markdown_links(
    line: str,
    base_url: str,
    seen_links: set[str],
    links: list[str],
) -> None:
    """Extract and deduplicate crawlable Markdown links from one line."""
    for link_match in _MD_LINK_RE.finditer(line):
        target = link_match.group(1).strip().strip("<>")
        if not _is_collectible_href(target):
            continue
        resolved, _ = urldefrag(urljoin(base_url, target))
        if resolved and resolved not in seen_links:
            seen_links.add(resolved)
            links.append(resolved)


def _fallback_title_from_url(url: str) -> str:
    """Derive a fallback page title from the trailing URL path segment or host."""
    parsed = urlparse(url)
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return segment or parsed.hostname or url


def _extract_markdown_meta_and_links(
    text: str,
    base_url: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Extract title, ATX headings, and outbound links from Markdown/text."""
    headings: list[str] = []
    links: list[str] = []
    seen_links: set[str] = set()
    first_h1 = ""
    in_fence = False
    fence_marker = ""

    for line in _postprocess_markdown(text).splitlines():
        in_fence, fence_marker, is_fence = _step_code_fence(line, in_fence, fence_marker)
        if is_fence or in_fence:
            continue
        h_match = _MD_ATX_HEADING_RE.match(line)
        if h_match:
            hashes, heading_text = h_match.group(1), h_match.group(2).strip()
            if heading_text:
                headings.append(f"{hashes} {heading_text}")
                if len(hashes) == 1 and not first_h1:
                    first_h1 = heading_text
        _collect_markdown_links(line, base_url, seen_links, links)

    return first_h1 or _fallback_title_from_url(base_url), tuple(headings), tuple(links)


def _summarize_markdown(markdown: str, title: str) -> str:
    """Create a short single-line summary from the first non-heading text."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", "---", "|", ">")):
            continue
        compact = re.sub(r"\s+", " ", stripped)
        return compact[:180]
    return title[:180]


def _parse_page_content(raw: _RawFetchedPage, depth: int) -> _FetchedPage:
    """Convert raw HTML or Markdown/plain text into a ``_FetchedPage``."""
    mime = _extract_mime_type(raw.content_type)
    if _is_passthrough_doc(raw.url, mime):
        markdown = _postprocess_markdown(raw.decoded_text)
        title, headings, links = _extract_markdown_meta_and_links(markdown, raw.url)
    else:
        parser = _DocsHtmlToMarkdownParser(base_url=raw.url)
        parser.feed(raw.decoded_text)
        parser.close()
        markdown = parser.get_markdown()
        title = parser.get_title() or _fallback_title_from_url(raw.url)
        headings = tuple(parser.headings)
        links = tuple(parser.links)

    if raw.truncated_by_bytes:
        notice = f"> [Truncated: response exceeded {MAX_BYTES_PER_PAGE} bytes limit]"
        markdown = f"{notice}\n\n{markdown}" if markdown else notice

    return _FetchedPage(
        url=raw.url,
        status_code=raw.status_code,
        content_type=mime or "text/html",
        title=title,
        markdown=markdown,
        headings=headings,
        summary=_summarize_markdown(markdown, title),
        links=links,
        depth=depth,
        truncated_by_bytes=raw.truncated_by_bytes,
    )


def _build_http_client(
    timeout_seconds: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Build a configured synchronous ``httpx.Client`` without auto-redirects."""
    return httpx.Client(
        verify=get_ssl_verify(),
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds),
        headers=_REQUEST_HEADERS,
        transport=transport,
    )


def _is_allowed_content_type(mime: str, url: str) -> bool:
    """Return True when MIME type or extension represents readable documentation."""
    if mime in _ALLOWED_MIME_TYPES:
        return True
    if not mime:
        ext = os.path.splitext(urlparse(url).path.lower())[1]
        return ext == "" or ext in _HTML_OR_EMPTY_ALLOWED_EXTS
    return False


def _resolve_redirect_target(current_url: str, response: httpx.Response) -> str:
    """Validate and canonicalize the redirect target from an HTTP response."""
    location = (response.headers.get("Location") or "").strip()
    if not location:
        raise ValueError(f"Redirect ({response.status_code}) missing Location header")
    joined = urljoin(current_url, location)
    _validate_url_syntax(joined)
    return _canonicalize_url(joined)


def _read_limited_response_bytes(response: httpx.Response) -> tuple[bytes, bool]:
    """Stream response chunks up to ``MAX_BYTES_PER_PAGE`` bytes."""
    chunks: list[bytes] = []
    total_bytes = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        remaining = MAX_BYTES_PER_PAGE - total_bytes
        if len(chunk) > remaining:
            if remaining > 0:
                chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        total_bytes += len(chunk)
    return b"".join(chunks), False


def _process_http_response(current_url: str, response: httpx.Response) -> _RawFetchedPage:
    """Validate status/content-type and decode a non-redirect HTTP response."""
    status_code = response.status_code
    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code} for {current_url}")
    content_type = response.headers.get("Content-Type") or ""
    mime = _extract_mime_type(content_type)
    if not _is_allowed_content_type(mime, current_url):
        raise ValueError(f"Unsupported content type {mime!r} for {current_url}")
    raw_bytes, truncated_by_bytes = _read_limited_response_bytes(response)
    decoded = _decode_response_bytes(
        raw_bytes,
        content_type,
        truncated_by_bytes=truncated_by_bytes,
    )
    return _RawFetchedPage(
        url=current_url,
        status_code=status_code,
        content_type=content_type,
        decoded_text=decoded,
        truncated_by_bytes=truncated_by_bytes,
    )


def _fetch_page_with_client(client: httpx.Client, url: str) -> _RawFetchedPage:
    """Fetch a single URL with manual SSRF-checked redirects and 1 MiB streaming cap."""
    current_url = url
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        _validate_url_and_dns(current_url)
        with client.stream("GET", current_url) as response:
            if response.status_code in _REDIRECT_STATUS_CODES:
                current_url = _resolve_redirect_target(current_url, response)
                continue
            return _process_http_response(current_url, response)
    raise RuntimeError(f"Exceeded max redirect hops ({_MAX_REDIRECT_HOPS})")


def _is_crawlable_candidate(
    url: str,
    *,
    allowed_netloc: str,
    path_prefix: str,
) -> bool:
    """Return True when ``url`` is an in-scope public HTTPS documentation page."""
    try:
        parsed = _validate_url_syntax(url)
    except ValueError:
        return False
    if _url_netloc(parsed) != allowed_netloc:
        return False
    if not _path_matches_prefix(parsed.path or "/", path_prefix):
        return False
    ext = os.path.splitext(parsed.path.lower())[1]
    if ext in _SKIP_EXTENSIONS:
        return False
    return True


def _cache_key(entry_url: str, effective_prefix: str) -> str:
    """Compute a deterministic cache directory key from entry URL and prefix."""
    payload = f"{entry_url}\n{effective_prefix}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _sanitize_slug(url: str) -> str:
    """Build a filesystem-safe slug from a URL path and query."""
    parsed = urlparse(url)
    raw = (parsed.path.strip("/") or "index")
    if parsed.query:
        raw = f"{raw}-{parsed.query}"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return (slug[:64] or "page").lower()


def _unlink_quietly(path: str | Path) -> None:
    """Best-effort file unlink ignoring missing or locked files."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _atomic_write_text(target: Path, content: str) -> None:
    """Atomically write UTF-8 text via same-directory temp file and ``os.replace``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    succeeded = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            delete=False,
        ) as handle:
            tmp_path = handle.name
            handle.write(content)
            handle.flush()
        os.replace(tmp_path, target)
        succeeded = True
    finally:
        if not succeeded and tmp_path and os.path.exists(tmp_path):
            _unlink_quietly(tmp_path)


def _write_cache_pages(
    cache_dir: Path,
    pages: list[_FetchedPage],
) -> tuple[set[Path], list[dict[str, Any]]]:
    """Write fetched page Markdown files and build ``index.json`` page records."""
    resolved_root = cache_dir.resolve()
    written_files: set[Path] = set()
    page_records: list[dict[str, Any]] = []
    for idx, page in enumerate(pages, start=1):
        rel_path = f"pages/{idx:04d}_{_sanitize_slug(page.url)}.md"
        target = (cache_dir / rel_path).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"Refusing to write cache outside {resolved_root}")
        _atomic_write_text(target, page.markdown)
        written_files.add(target)
        page_records.append(
            {
                "url": page.url,
                "depth": page.depth,
                "status_code": page.status_code,
                "content_type": page.content_type,
                "title": page.title,
                "headings": list(page.headings),
                "summary": page.summary,
                "file": rel_path,
                "char_count": len(page.markdown),
                "truncated_by_bytes": page.truncated_by_bytes,
            }
        )
    return written_files, page_records


def _prune_stale_cache_pages(pages_dir: Path, written_files: set[Path]) -> None:
    """Remove obsolete Markdown files left over from an earlier crawl."""
    for existing in pages_dir.glob("*.md"):
        if existing.resolve() not in written_files:
            _unlink_quietly(existing)


def _persist_cache(cache_dir: Path, result: _CrawlResult) -> None:
    """Persist fetched pages and ``index.json`` atomically to local cache."""
    pages_dir = cache_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    written_files, page_records = _write_cache_pages(cache_dir, result.pages)
    _prune_stale_cache_pages(pages_dir, written_files)

    now = time.time()
    index_payload = {
        "version": _INDEX_VERSION,
        "entry_url": result.req.url,
        "final_entry_url": result.final_entry_url,
        "path_prefix": result.effective_prefix,
        "fetched_at": now,
        "fetched_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "max_depth": result.req.max_depth,
        "max_pages": result.req.max_pages,
        "truncated_by_max_pages": result.truncated_by_max_pages,
        "pages": page_records,
        "errors": result.errors,
    }
    _atomic_write_text(
        cache_dir / "index.json",
        json.dumps(index_payload, ensure_ascii=False, indent=2),
    )


def _read_index_json(cache_dir: Path) -> dict[str, Any] | None:
    """Read and parse ``index.json`` from ``cache_dir`` if present and valid."""
    index_path = cache_dir / "index.json"
    if not index_path.is_file():
        return None
    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(index_data, dict):
        return None
    if index_data.get("version") != _INDEX_VERSION:
        return None
    return index_data


def _is_index_fresh(index_data: dict[str, Any], req: FetchDocsRequest) -> bool:
    """Return True when cached metadata covers ``req`` within ``req.ttl_seconds``."""
    try:
        fetched_at = float(index_data["fetched_at"])
        cached_max_depth = int(index_data["max_depth"])
        cached_max_pages = int(index_data["max_pages"])
    except (KeyError, TypeError, ValueError):
        return False
    age = time.time() - fetched_at
    if age < 0 or age > req.ttl_seconds:
        return False
    if cached_max_depth < req.max_depth:
        return False
    truncated = bool(index_data.get("truncated_by_max_pages", False))
    return not (cached_max_pages < req.max_pages and truncated)


def _read_cached_page_record(
    cache_dir: Path,
    resolved_root: Path,
    item: Any,
) -> _FetchedPage | None:
    """Load one cached page entry from disk, returning None on invalid data."""
    if not isinstance(item, dict):
        return None
    rel_file = str(item.get("file") or "")
    if not rel_file:
        return None
    page_path = (cache_dir / rel_file).resolve()
    if not page_path.is_relative_to(resolved_root) or not page_path.is_file():
        return None
    try:
        markdown = page_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        status_code = int(item.get("status_code", 200))
        depth = int(item.get("depth", 0))
    except (ValueError, TypeError):
        return None
    return _FetchedPage(
        url=str(item.get("url") or ""),
        status_code=status_code,
        content_type=str(item.get("content_type") or "text/html"),
        title=str(item.get("title") or ""),
        markdown=markdown,
        headings=tuple(str(h) for h in (item.get("headings") or ())),
        summary=str(item.get("summary") or ""),
        links=(),
        depth=depth,
        truncated_by_bytes=bool(item.get("truncated_by_bytes", False)),
    )


def _load_cached_pages(
    cache_dir: Path,
    raw_pages: list[Any],
    max_depth: int,
) -> list[_FetchedPage] | None:
    """Load all cached page records within ``max_depth``, or None if corrupted."""
    resolved_root = cache_dir.resolve()
    loaded_pages: list[_FetchedPage] = []
    for item in raw_pages:
        page = _read_cached_page_record(cache_dir, resolved_root, item)
        if page is None:
            return None
        if page.depth <= max_depth:
            loaded_pages.append(page)
    return loaded_pages or None


def _try_load_cache(
    *,
    cache_dir: Path,
    req: FetchDocsRequest,
) -> tuple[dict[str, Any], list[_FetchedPage]] | None:
    """Return cached index and pages if valid and fresh, otherwise None."""
    if req.refresh or req.ttl_seconds <= 0:
        return None
    index_data = _read_index_json(cache_dir)
    if index_data is None or not _is_index_fresh(index_data, req):
        return None
    raw_pages = index_data.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        return None
    loaded_pages = _load_cached_pages(cache_dir, raw_pages, req.max_depth)
    if loaded_pages is None:
        return None
    return index_data, loaded_pages[: req.max_pages]


def _flush_markdown_section(
    sections: list[tuple[str, str]],
    heading: str,
    lines: list[str],
) -> None:
    """Append a non-empty ``(heading, body)`` section to ``sections``."""
    body = "\n".join(lines).strip()
    if body or heading:
        sections.append((heading, body))


def _split_markdown_sections(markdown: str) -> list[tuple[str, str]]:
    """Split Markdown into ``(heading, section_body)`` pairs outside code fences."""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in markdown.splitlines():
        in_fence, fence_marker, is_fence = _step_code_fence(line, in_fence, fence_marker)
        if is_fence or in_fence:
            current_lines.append(line)
            continue
        h_match = _MD_ATX_HEADING_RE.match(line)
        if h_match:
            if current_lines:
                _flush_markdown_section(sections, current_heading, current_lines)
            current_heading = h_match.group(2).strip()
            current_lines = [line]
            continue
        current_lines.append(line)

    if current_lines or current_heading:
        _flush_markdown_section(sections, current_heading, current_lines)
    return sections


def _tokenize_query(query: str) -> list[str]:
    """Tokenize a query into unique lowercased search terms."""
    raw_tokens = _QUERY_TOKEN_RE.findall(query.lower())
    if not raw_tokens:
        raw_tokens = query.lower().split()
    return list(dict.fromkeys(tok for tok in raw_tokens if tok))


def _score_section(
    title: str,
    heading: str,
    body: str,
    query_spec: tuple[str, list[str]],
) -> int:
    """Score one section against ``(phrase, tokens)`` by title and body match."""
    phrase, tokens = query_spec
    title_hay = f"{title}\n{heading}".lower()
    body_hay = body.lower()
    score = 0
    if phrase in title_hay:
        score += 10
    if phrase in body_hay:
        score += 5
    for tok in tokens:
        if tok in title_hay:
            score += 3
        if tok in body_hay:
            score += 1
    return score


def _rank_query_sections(
    pages: list[_FetchedPage],
    query: str,
) -> list[tuple[int, int, int, _FetchedPage, str, str]]:
    """Score and rank all Markdown sections across ``pages`` for ``query``."""
    query_spec = (query.lower(), _tokenize_query(query))
    scored: list[tuple[int, int, int, _FetchedPage, str, str]] = []
    for page_idx, page in enumerate(pages):
        sections = _split_markdown_sections(page.markdown) or [(page.title, page.markdown)]
        for sec_idx, (heading, body) in enumerate(sections):
            score = _score_section(page.title, heading, body, query_spec)
            if score > 0:
                scored.append((score, -page_idx, -sec_idx, page, heading, body))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return scored


def _render_query_results(
    *,
    header_lines: list[str],
    index_lines: list[str],
    pages: list[_FetchedPage],
    query: str,
    max_chars: int,
) -> str:
    """Rank sections against ``query`` and render up to ``max_chars``."""
    scored = _rank_query_sections(pages, query)
    parts = [*header_lines, "", *index_lines]
    if not scored:
        parts.append(f"\nNo sections matched query: {query}")
        return _clip_to_max_chars("\n".join(parts), max_chars)

    parts.append(f"\n## Query Matches ({len(scored)} sections for {query!r})")
    for score, _p, _s, page, heading, body in scored:
        sec_title = heading or page.title
        parts.append(
            f"\n### {page.title} — {sec_title} (score={score})\n"
            f"URL: {page.url}\n\n{body}"
        )
    return _clip_to_max_chars("\n".join(parts), max_chars)


def _clip_to_max_chars(text: str, max_chars: int) -> str:
    """Clip output text cleanly at ``max_chars`` with an explicit truncation notice."""
    if len(text) <= max_chars:
        return text
    notice = f"\n\n[Truncated at max_chars={max_chars}]"
    budget = max(0, max_chars - len(notice))
    return text[:budget].rstrip() + notice


def _render_output(result: _CrawlResult, cache_status: str, cache_dir: Path) -> str:
    """Render metadata header, page index, and body or query matches."""
    req = result.req
    header_lines = [
        f"Source: {result.final_entry_url}",
        f"Cache: {cache_status} ({cache_dir})",
        f"Scope: prefix={result.effective_prefix} | max_depth={req.max_depth} | "
        f"pages={len(result.pages)}/{req.max_pages}",
    ]
    if result.errors:
        header_lines.append(f"Errors ({len(result.errors)}): " + "; ".join(result.errors[:5]))

    index_lines = ["## Index"]
    for idx, page in enumerate(result.pages, start=1):
        top_headings = ", ".join(page.headings[:4])
        heading_note = f" | headings: {top_headings}" if top_headings else ""
        index_lines.append(
            f"{idx}. [{page.title}]({page.url}) (depth={page.depth}){heading_note}"
        )

    if req.query:
        return _render_query_results(
            header_lines=header_lines,
            index_lines=index_lines,
            pages=result.pages,
            query=req.query,
            max_chars=req.max_chars,
        )

    sections = [*header_lines, "", *index_lines]
    for page in result.pages:
        sections.append(
            f"\n---\n## {page.title}\nURL: {page.url} (depth={page.depth})\n\n"
            f"{page.markdown}"
        )
    return _clip_to_max_chars("\n".join(sections), req.max_chars)


def _init_crawl_state(
    req: FetchDocsRequest,
    initial_parsed: ParseResult,
    effective_prefix: str,
) -> _CrawlState:
    """Initialize mutable BFS state for a fresh crawl."""
    return _CrawlState(
        req=req,
        effective_prefix=effective_prefix,
        allowed_netloc=_url_netloc(initial_parsed),
        final_entry_url=req.url,
        fetched_pages=[],
        errors=[],
        visited={req.url},
        indexed_urls=set(),
        queue=deque([(req.url, 0)]),
    )


def _update_scope_after_fetch(
    state: _CrawlState,
    current_url: str,
    final_url: str,
    depth: int,
) -> bool:
    """Update crawl scope at ``depth == 0`` or verify redirected child stays in scope."""
    final_parsed = urlparse(final_url)
    if depth == 0:
        state.final_entry_url = final_url
        state.allowed_netloc = _url_netloc(final_parsed)
        final_path = final_parsed.path or "/"
        if state.req.path_prefix is None:
            state.effective_prefix = _derive_path_prefix(final_path)
            return True
        if not _path_matches_prefix(final_path, state.effective_prefix):
            raise ValueError(
                f"Redirected entry path {final_path!r} outside "
                f"path_prefix {state.effective_prefix!r}"
            )
        return True

    in_host = _url_netloc(final_parsed) == state.allowed_netloc
    in_prefix = _path_matches_prefix(final_parsed.path or "/", state.effective_prefix)
    if not in_host or not in_prefix:
        state.errors.append(f"Skipped {current_url}: redirected outside scope to {final_url}")
        return False
    return True


def _enqueue_page_links(
    state: _CrawlState,
    links: tuple[str, ...],
    depth: int,
) -> None:
    """Enqueue unseen in-scope child links when ``depth < max_depth``."""
    if depth >= state.req.max_depth:
        return
    for raw_link in links:
        if not _is_crawlable_candidate(
            raw_link,
            allowed_netloc=state.allowed_netloc,
            path_prefix=state.effective_prefix,
        ):
            continue
        canonical_link = _canonicalize_url(raw_link)
        if canonical_link not in state.visited:
            state.visited.add(canonical_link)
            state.queue.append((canonical_link, depth + 1))


def _process_crawl_step(
    client: httpx.Client,
    state: _CrawlState,
    current_url: str,
    depth: int,
) -> None:
    """Fetch, validate scope, parse, and enqueue links for one BFS queue item."""
    try:
        raw_page = _fetch_page_with_client(client, current_url)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if depth == 0:
            raise
        state.errors.append(f"{current_url}: {exc}")
        return

    state.visited.add(raw_page.url)
    if not _update_scope_after_fetch(state, current_url, raw_page.url, depth):
        return
    if raw_page.url in state.indexed_urls:
        return
    page = _parse_page_content(raw_page, depth)
    state.fetched_pages.append(page)
    state.indexed_urls.add(raw_page.url)
    _enqueue_page_links(state, page.links, depth)


def _crawl_bfs_pages(
    req: FetchDocsRequest,
    initial_parsed: ParseResult,
    effective_prefix: str,
    transport: httpx.BaseTransport | None,
) -> _CrawlResult:
    """Execute bounded BFS crawl using a shared HTTP client."""
    state = _init_crawl_state(req, initial_parsed, effective_prefix)
    with _build_http_client(req.timeout_seconds, transport=transport) as client:
        while state.queue and len(state.fetched_pages) < req.max_pages:
            current_url, depth = state.queue.popleft()
            _process_crawl_step(client, state, current_url, depth)
    return _CrawlResult(
        req=req,
        final_entry_url=state.final_entry_url,
        effective_prefix=state.effective_prefix,
        pages=state.fetched_pages,
        errors=state.errors,
        truncated_by_max_pages=bool(state.queue),
    )


def _fetch_docs_sync(
    req: FetchDocsRequest,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Execute cache lookup, SSRF-checked BFS crawl, and Markdown rendering."""
    initial_parsed = urlparse(req.url)
    effective_prefix = req.path_prefix or _derive_path_prefix(initial_parsed.path or "/")
    cache_dir = get_fetch_docs_cache_dir() / _cache_key(req.url, req.path_prefix or "")

    cached = _try_load_cache(cache_dir=cache_dir, req=req)
    if cached is not None:
        index_data, cached_pages = cached
        cached_result = _CrawlResult(
            req=req,
            final_entry_url=str(index_data.get("final_entry_url") or req.url),
            effective_prefix=str(index_data.get("path_prefix") or effective_prefix),
            pages=cached_pages,
            errors=[str(err) for err in (index_data.get("errors") or ())],
        )
        return _render_output(cached_result, "hit", cache_dir)

    result = _crawl_bfs_pages(req, initial_parsed, effective_prefix, transport)
    _persist_cache(cache_dir, result)
    return _render_output(result, "miss", cache_dir)


@tool(
    name="fetch_docs",
    description=(
        "Fetch and index technical documentation from a public HTTPS URL into clean Markdown, "
        "crawling same-host links within the path prefix and caching results locally. "
        "Supports optional keyword filtering via `query` against titles, headings, and sections. "
        "Use `refresh=True` to bypass the local cache."
    ),
)
async def fetch_docs(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    url: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_chars: int = DEFAULT_MAX_CHARS,
    refresh: bool = False,
    query: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    path_prefix: str | None = None,
) -> str:
    """Fetch, convert, and locally cache technical documentation pages.

    Args:
        url: Public HTTPS entry URL of the documentation page or section.
        max_depth: Link crawl depth from the entry URL (0-3, default 1).
        max_pages: Maximum pages to fetch per crawl (1-30, default 10).
        timeout_seconds: Per-request HTTP timeout in seconds (5-60, default 15).
        max_chars: Maximum output characters returned (1000-100000, default 30000).
        refresh: Whether to bypass fresh cached results and re-fetch (default False).
        query: Optional search terms to filter cached or fetched sections by relevance.
        ttl_seconds: Cache freshness window in seconds (0-604800, default 3600).
        path_prefix: Optional URL path prefix (e.g. '/3/library/') scoping recursion.
    """
    try:
        req = _normalize_request(
            {
                "url": url,
                "max_depth": max_depth,
                "max_pages": max_pages,
                "timeout_seconds": timeout_seconds,
                "max_chars": max_chars,
                "refresh": refresh,
                "query": query,
                "ttl_seconds": ttl_seconds,
                "path_prefix": path_prefix,
            }
        )
        return await asyncio.to_thread(_fetch_docs_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: fetch_docs failed: {exc}"
