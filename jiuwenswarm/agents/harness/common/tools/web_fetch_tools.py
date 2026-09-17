# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web fetch tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import os
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import requests
import urllib3
from openjiuwen.core.foundation.tool import tool

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}
_FREE_SEARCH_PROXY_URL_ENV = "FREE_SEARCH_PROXY_URL"
_FREE_SEARCH_SSL_VERIFY_ENV = "FREE_SEARCH_SSL_VERIFY"
_FREE_SEARCH_DEFAULT_NO_PROXY = (
    "127.0.0.1,.huawei.com,localhost,local,.local,10.155.97.247,.myhuaweicloud.com"
)
_CHARSET_HEADER_RE = re.compile(r"charset=([^\s;]+)", flags=re.IGNORECASE)
_CHARSET_META_RE = re.compile(
    br"""<meta[^>]+charset=["']?\s*([A-Za-z0-9._-]+)""",
    flags=re.IGNORECASE,
)


def _extract_declared_charset(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "") or ""
    header_match = _CHARSET_HEADER_RE.search(content_type)
    if header_match:
        return header_match.group(1).strip().strip("\"'")

    head_bytes = (response.content or b"")[:4096]
    meta_match = _CHARSET_META_RE.search(head_bytes)
    if meta_match:
        try:
            return meta_match.group(1).decode("ascii", errors="ignore").strip()
        except Exception:
            return ""
    return ""


def _get_free_search_proxy_url() -> str:
    return str(os.environ.get(_FREE_SEARCH_PROXY_URL_ENV, "") or "").strip()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _free_search_ssl_verify() -> bool:
    return _env_bool(_FREE_SEARCH_SSL_VERIFY_ENV, default=False)


def _disable_insecure_request_warning() -> None:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _no_proxy_entries() -> list[str]:
    configured = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or _FREE_SEARCH_DEFAULT_NO_PROXY
    return [entry.strip().lower() for entry in configured.split(",") if entry.strip()]


def _should_bypass_free_search_proxy(url: str) -> bool:
    proxy_url = _get_free_search_proxy_url()
    if not proxy_url:
        return True
    hostname = (urlparse(url).hostname or "").lower()
    if not hostname:
        return False
    for entry in _no_proxy_entries():
        if entry == "*":
            return True
        if entry.startswith(".") and (hostname == entry[1:] or hostname.endswith(entry)):
            return True
        if hostname == entry or hostname.endswith(f".{entry}"):
            return True
    return False


def _apply_free_search_proxy(url: str, kwargs: dict[str, object]) -> bool:
    proxy_url = _get_free_search_proxy_url()
    if not proxy_url or _should_bypass_free_search_proxy(url):
        return False
    kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})
    return True


def _decode_response_text(response: requests.Response) -> str:
    raw = response.content or b""
    if not raw:
        return ""

    declared = (_extract_declared_charset(response) or "").lower()
    response_encoding = (response.encoding or "").strip().lower()
    apparent = (response.apparent_encoding or "").strip().lower()

    # Prefer explicit non-latin declaration first; then utf-8; then heuristics.
    candidates: list[str] = []
    if declared and declared not in {"iso-8859-1", "latin-1", "latin1"}:
        candidates.append(declared)

    candidates.extend(
        [
            "utf-8",
            apparent,
            response_encoding,
            "gb18030",
            "big5",
            "shift_jis",
            "cp1252",
            "iso-8859-1",
        ]
    )

    seen: set[str] = set()
    for enc in candidates:
        enc = (enc or "").strip().lower()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue

    # Last-resort fallback.
    return raw.decode("utf-8", errors="replace")


def _http_get(url: str, **kwargs) -> requests.Response:
    """Try normal requests first; retry without env proxies on ProxyError."""
    explicit_proxy = _apply_free_search_proxy(url, kwargs)
    verify = _free_search_ssl_verify()
    kwargs.setdefault("verify", verify)
    if verify is False:
        _disable_insecure_request_warning()
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.ProxyError:
        if explicit_proxy:
            raise
        with requests.Session() as session:
            session.trust_env = False
            return session.get(url, **kwargs)


def _clip_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return unescape(re.sub(r"\s+", " ", value)).strip()


def _decode_ddg_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path != "/l/":
        return url
    query = parse_qs(parsed.query)
    target = query.get("uddg")
    if not target:
        return url
    return unquote(target[0])


def _normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    decoded = _decode_ddg_redirect(raw)
    if decoded.startswith(("http://", "https://")):
        return decoded
    return f"https://{decoded}"


def _fetch_via_jina_reader_sync(url: str, timeout_seconds: int) -> dict[str, str | int]:
    reader_url = f"https://r.jina.ai/{url}"
    response = _http_get(reader_url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    return {
        "url": url,
        "status_code": response.status_code,
        "title": "",
        "content": _decode_response_text(response).strip(),
    }


def _fetch_webpage_sync(url: str, timeout_seconds: int) -> dict[str, str | int]:
    response = _http_get(url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
    if response.status_code in {401, 403, 429}:
        return _fetch_via_jina_reader_sync(url, timeout_seconds)
    response.raise_for_status()

    text = _decode_response_text(response)
    content_type = response.headers.get("Content-Type", "")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    title = _strip_tags(title_match.group(1)) if title_match else ""

    if "html" in content_type.lower():
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = _strip_tags(text)
    else:
        text = re.sub(r"\s+", " ", text).strip()

    return {
        "url": response.url,
        "status_code": response.status_code,
        "title": title,
        "content": text,
    }


@tool(
    name="mcp_fetch_webpage",
    description=(
        "Fetch webpage text content from URL. Returns status/title/plain text content. "
        "Set max_chars=0 to disable output clipping. "
        "Use a larger timeout_seconds for slow websites."
    ),
)
async def mcp_fetch_webpage(url: str, max_chars: int = 0, timeout_seconds: int = 30) -> str:
    url = _normalize_url(url)
    if not url:
        return "[ERROR]: url cannot be empty."

    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 0
    if max_chars < 0:
        max_chars = 0

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 30
    try:
        max_timeout_seconds = int(os.getenv("MCP_FETCH_WEBPAGE_MAX_TIMEOUT_SECONDS") or "3600")
    except ValueError:
        max_timeout_seconds = 3600
    max_timeout_seconds = max(1, max_timeout_seconds)
    timeout_seconds = max(1, min(timeout_seconds, max_timeout_seconds))

    try:
        data = await asyncio.to_thread(_fetch_webpage_sync, url, timeout_seconds)
    except Exception as exc:
        return f"[ERROR]: failed to fetch webpage: {exc}"

    lines = [
        f"URL: {data.get('url', url)}",
        f"Status: {data.get('status_code', '')}",
    ]
    if data.get("title"):
        lines.append(f"Title: {data['title']}")
    lines.append("Content:")
    lines.append(_clip_text(str(data.get("content", "") or ""), max_chars) or "[empty]")
    return "\n".join(lines)
