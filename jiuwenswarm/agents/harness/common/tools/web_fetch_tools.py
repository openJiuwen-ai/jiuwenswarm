# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web fetch tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import urllib3
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.http_proxy_config import requests_get
from jiuwenswarm.common.local_env_config import get_local_config

logger = logging.getLogger(__name__)
# 独立 baseline logger，命名空间挂在 jiuwenswarm 日志树下，避免被日志框架丢弃。
# 日志行以 "BASELINE " 前缀开头，便于 grep 摸底。
baseline = logging.getLogger("jiuwenswarm.agents.harness.common.tools.web_fetch_tools.baseline")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}
_FREE_SEARCH_PROXY_URL_ENV = "FREE_SEARCH_PROXY_URL"
_FREE_SEARCH_SSL_VERIFY_ENV = "FREE_SEARCH_SSL_VERIFY"
_JINA_FETCH_ENABLED_ENV = "JIUWENSWARM_ENABLE_JINA_FETCH"
_FREE_SEARCH_DEFAULT_NO_PROXY = (
    "127.0.0.1,.huawei.com,localhost,local,.local,10.155.97.247,.myhuaweicloud.com"
)
_CHARSET_HEADER_RE = re.compile(r"charset=([^\s;]+)", flags=re.IGNORECASE)
_CHARSET_META_RE = re.compile(
    br"""<meta[^>]+charset=["']?\s*([A-Za-z0-9._-]+)""",
    flags=re.IGNORECASE,
)
# Non-webpage binaries that historically leaked as mojibake via text decode.
_BINARY_URL_SUFFIXES = (
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".tar",
    ".tgz",
    ".exe",
    ".dll",
    ".msi",
    ".dmg",
    ".apk",
    ".iso",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".mkv",
    ".wav",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
)
_BINARY_CONTENT_TYPE_MARKERS = (
    "application/msword",
    "application/pdf",
    "application/zip",
    "application/x-zip",
    "application/gzip",
    "application/x-gzip",
    "application/x-rar",
    "application/x-7z-compressed",
    "application/vnd.openxmlformats",
    "application/vnd.ms-",
    "application/vnd.oasis",
    "image/",
    "audio/",
    "video/",
    "font/",
)
_BINARY_MAGIC_PREFIXES = (
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole/cfbf"),
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf'\x1c", "7z"),
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
    return str(get_local_config(_FREE_SEARCH_PROXY_URL_ENV, "") or "").strip()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(get_local_config(name, "") or "").strip().lower()
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


def _http_get(url: str, **kwargs) -> Any:
    """HTTP GET via overlay-aware proxy helpers; free-search proxy still applied."""
    _apply_free_search_proxy(url, kwargs)
    verify = _free_search_ssl_verify()
    kwargs.setdefault("verify", verify)
    if verify is False:
        _disable_insecure_request_warning()
    return requests_get(url, **kwargs)


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


def _binary_url_reason(url: str) -> str:
    """Return a short reason when the URL path looks like a non-webpage binary."""
    path = unquote(urlparse(url).path or "").lower()
    for suffix in _BINARY_URL_SUFFIXES:
        if path.endswith(suffix):
            return f"url ends with {suffix}"
    return ""


def _binary_magic_reason(raw: bytes) -> str:
    if not raw:
        return ""
    for magic, name in _BINARY_MAGIC_PREFIXES:
        if raw.startswith(magic):
            return f"magic:{name}"
    return ""


def _looks_like_textual_payload(raw: bytes) -> bool:
    """Best-effort sniff for text/HTML when Content-Type is octet-stream."""
    sample = (raw or b"")[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not sample:
        return True
    if sample.startswith(
        (b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML", b"<?xml", b"{", b"[")
    ):
        return True
    if sample.count(b"\x00") > 2:
        return False
    for enc in ("utf-8", "gb18030"):
        try:
            text = sample.decode(enc)
        except UnicodeDecodeError:
            continue
        if not text:
            return True
        printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
        return (printable / len(text)) >= 0.9
    return False


def _binary_content_type_reason(content_type: str, raw: bytes) -> str:
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    if not ct:
        return ""
    if (
        "html" in ct
        or ct.startswith("text/")
        or ct
        in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/xhtml+xml",
            "image/svg+xml",
        }
    ):
        return ""
    if ct == "application/octet-stream":
        magic = _binary_magic_reason(raw)
        if magic:
            return f"content-type={ct}; {magic}"
        if _looks_like_textual_payload(raw):
            return ""
        return f"content-type={ct}"
    for marker in _BINARY_CONTENT_TYPE_MARKERS:
        if marker in ct:
            return f"content-type={ct}"
    return ""


def _unsupported_binary_reason(
    *,
    url: str,
    content_type: str = "",
    raw: bytes | None = None,
) -> str:
    """Detect non-text payloads that must not be decoded as webpage text."""
    url_reason = _binary_url_reason(url)
    if url_reason:
        return url_reason
    if raw is None:
        return ""
    magic = _binary_magic_reason(raw)
    if magic:
        return magic
    return _binary_content_type_reason(content_type, raw)


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
    binary_reason = _binary_url_reason(url)
    if binary_reason:
        raise ValueError(f"unsupported binary content ({binary_reason})")

    response = _http_get(url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
    if response.status_code in {401, 403, 429} and _env_bool(
        _JINA_FETCH_ENABLED_ENV,
        default=False,
    ):
        return _fetch_via_jina_reader_sync(url, timeout_seconds)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "") or ""
    raw = response.content or b""
    binary_reason = _unsupported_binary_reason(url=url, content_type=content_type, raw=raw)
    final_url = str(response.url or "")
    if not binary_reason and final_url and final_url != url:
        binary_reason = _binary_url_reason(final_url)
    if binary_reason:
        raise ValueError(f"unsupported binary content ({binary_reason})")

    text = _decode_response_text(response)
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


async def _fetch_single_url(
    raw_url: str,
    *,
    max_chars: int,
    timeout_seconds: int,
    use_cache: bool,
    cache: Any | None,
) -> dict[str, Any]:
    """Fetch a single URL and return one result-item dict.

    The returned dict always carries the (normalized) ``url`` key plus either
    success fields (``status_code``/``title``/``content``/``provider``/
    ``from_cache``...) or an ``error`` field when fetching failed.
    """
    url = _normalize_url(raw_url)
    if not url:
        return {
            "url": str(raw_url or "").strip(),
            "status_code": None,
            "title": "",
            "content": "",
            "provider": "",
            "from_cache": False,
            "error": "url cannot be empty.",
        }

    binary_reason = _binary_url_reason(url)
    if binary_reason:
        return {
            "url": url,
            "status_code": None,
            "title": "",
            "content": "",
            "provider": "",
            "from_cache": False,
            "error": f"unsupported binary content ({binary_reason})",
        }

    # 模式 1：查缓存
    if cache is not None and use_cache:
        cached = await cache.get(url)
        if cached is not None:
            now = time.time()
            cache_age_days = round((now - float(cached.cached_at or 0)) / 86400, 1)
            update_time = cached.update_time
            page_update_days = None
            if update_time is not None:
                page_update_days = round((now - float(update_time)) / 86400, 1)
            return {
                "url": cached.url,
                "status_code": 200,
                "title": cached.title or "",
                "content": _clip_text(str(cached.content or ""), max_chars) or "[empty]",
                "provider": f"cache:{cached.source or 'unknown'}",
                "from_cache": True,
                "cache_age_days": cache_age_days,
                "page_update_days": page_update_days,
            }
    elif cache is not None and not use_cache:
        cache.bypassed += 1

    # 模式 2：实时抓取（不回写缓存）
    _fetch_start = time.perf_counter()
    try:
        data = await asyncio.to_thread(_fetch_webpage_sync, url, timeout_seconds)
    except Exception as exc:
        cost_ms = int((time.perf_counter() - _fetch_start) * 1000)
        baseline.info(
            "BASELINE fetch url=%s ok=N cost_ms=%d",
            url,
            cost_ms,
        )
        reason = str(exc).strip() or "unknown error"
        return {
            "url": url,
            "status_code": None,
            "title": "",
            "content": "",
            "provider": "",
            "from_cache": False,
            "error": f"fetch failed ({reason})",
        }

    cost_ms = int((time.perf_counter() - _fetch_start) * 1000)
    content_chars = len(str(data.get("content", "") or ""))
    baseline.info(
        "BASELINE fetch url=%s ok=Y cost_ms=%d chars=%d provider=direct",
        url,
        cost_ms,
        content_chars,
    )

    return {
        "url": data.get("url", url),
        "status_code": data.get("status_code"),
        "title": data.get("title", ""),
        "content": _clip_text(str(data.get("content", "") or ""), max_chars) or "[empty]",
        "provider": "direct",
        "from_cache": False,
    }


def _coerce_url_list(url: str | list[str]) -> list[str]:
    """Normalize the ``url`` argument into a flat list of URL strings."""
    if url is None:
        return []
    if isinstance(url, str):
        return [url] if url.strip() else []
    if isinstance(url, (list, tuple)):
        return [str(u).strip() for u in url if str(u).strip()]
    return [str(url).strip()] if str(url).strip() else []


async def mcp_fetch_webpage_impl(
    url: str | list[str],
    max_chars: int = 0,
    timeout_seconds: int = 30,
    use_cache: bool = True,
    cache: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch one or more webpages.

    ``url`` accepts either a single URL string or a list of URLs. Each URL is
    fetched concurrently (cache-first when ``use_cache`` is true). Returns a
    list of per-URL items preserving the input order.
    """
    urls = _coerce_url_list(url)
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

    if not urls:
        if cache is not None:
            _log_cache_stats(cache)
        return []

    tasks = [
        _fetch_single_url(
            u,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
            use_cache=use_cache,
            cache=cache,
        )
        for u in urls
    ]
    items = await asyncio.gather(*tasks)

    if cache is not None:
        _log_cache_stats(cache)

    return list(items)


mcp_fetch_webpage = tool(
    name="mcp_fetch_webpage",
    description=(
        "抓取网页文本内容，支持一次传入多个 URL 并行抓取。"
        "默认优先从内存缓存读取（use_cache=true），"
        "若缓存内容不够新或需要最新数据，请用 use_cache=false 重新从原站抓取。"
        "返回列表，每个元素含一个 URL 的状态码、标题、纯文本正文与是否命中缓存。"
    ),
)(mcp_fetch_webpage_impl)


def _log_cache_stats(cache: Any) -> None:
    """输出缓存命中率汇总日志（与 enterprise_dev [FetchCache] 风格对齐）。"""
    try:
        stats = cache.stats()
        logger.info(
            "[FetchCache] hits=%d misses=%d bypassed=%d hit_rate=%.1f%% entries=%d",
            stats["hits"],
            stats["misses"],
            stats["bypassed"],
            stats["hit_rate_pct"],
            stats["entries"],
        )
    except Exception:
        logger.debug("[FetchCache] stats log failed", exc_info=True)
