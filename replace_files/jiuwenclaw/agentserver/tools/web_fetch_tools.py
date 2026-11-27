# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web fetch tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import trafilatura
from openjiuwen.core.foundation.tool import ToolCard, tool

from jiuwenclaw.agentserver.tools.ssl_config import get_requests_verify

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/markdown, text/html;q=0.9, */*;q=0.1",
    "Accept-Language": "en-US,en;q=0.9",
}
_CHARSET_HEADER_RE = re.compile(r"charset=([^\s;]+)", flags=re.IGNORECASE)
_CHARSET_META_RE = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([A-Za-z0-9._-]+)""",
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
        except Exception:
            continue

    # Last-resort fallback.
    return raw.decode("utf-8", errors="replace")


def _http_get(url: str, **kwargs) -> requests.Response:
    """Try normal requests first; retry without env proxies on ProxyError."""
    kwargs.setdefault("verify", get_requests_verify())
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.get(url, **kwargs)


def _clip_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
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


def _extract_content_with_trafilatura(
    html: str, url: str, content_type: str, extract_mode: str = "markdown"
) -> tuple[str, str]:
    """Extract main content using trafilatura with fallback to basic cleaning.

    Returns:
        tuple of (content, title)
    """
    content_type_lower = (content_type or "").lower()

    if "application/json" in content_type_lower:
        try:
            return json.dumps(json.loads(html), indent=2, ensure_ascii=False), ""
        except Exception:
            return html, ""

    if "text/markdown" in content_type_lower or "text/x-markdown" in content_type_lower:
        return html, ""

    if "text/html" in content_type_lower or not content_type_lower:
        try:
            metadata = trafilatura.extract_metadata(html, default_url=url)
            title = metadata.title if metadata else ""
        except Exception:
            title = ""

        try:
            output_format = "markdown" if extract_mode == "markdown" else "text"
            result = trafilatura.extract(
                html,
                url=url,
                output_format=output_format,
                include_links=True,
                include_images=False,
                favor_precision=True,
            )
            if result:
                return result, title
        except Exception:
            logger.warning(
                "Trafilatura extraction failed in precision mode, falling back",
                exc_info=True,
            )

        try:
            result = trafilatura.extract(
                html,
                url=url,
                output_format=extract_mode,
                include_links=False,
                include_images=False,
            )
            if result:
                return result, title
        except Exception:
            logger.warning(
                "Trafilatura extraction failed in fallback mode, using HTML cleanup",
                exc_info=True,
            )

        text = re.sub(
            r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(
            r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(
            r"<nav[^>]*>.*?</nav>", " ", text, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(
            r"<footer[^>]*>.*?</footer>", " ", text, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(
            r"<header[^>]*>.*?</header>", " ", text, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(
            r"<aside[^>]*>.*?</aside>", " ", text, flags=re.IGNORECASE | re.DOTALL
        )
        text = _strip_tags(text)

        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL
        )
        if not title and title_match:
            title = _strip_tags(title_match.group(1))

        return text, title

    text = re.sub(r"\s+", " ", html).strip()
    return text, ""


def _fetch_direct_sync(url: str, timeout_seconds: int) -> dict[str, Any] | None:
    """Direct HTTP request with content extraction."""
    try:
        response = _http_get(url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)

        if response.status_code >= 400:
            return None

        response.raise_for_status()

        html = _decode_response_text(response)
        content_type = response.headers.get("Content-Type", "")
        content, title = _extract_content_with_trafilatura(html, url, content_type)

        return {
            "url": response.url or url,
            "status_code": response.status_code,
            "title": title,
            "content": content,
            "provider": "direct",
        }
    except Exception:
        logger.warning("Direct webpage fetch failed", exc_info=True)
        return None


def _fetch_via_jina_sync(url: str, timeout_seconds: int) -> dict[str, Any] | None:
    """Fetch via Jina Reader proxy."""
    try:
        reader_url = f"https://r.jina.ai/{url}"
        response = _http_get(
            reader_url, headers=_REQUEST_HEADERS, timeout=timeout_seconds
        )
        response.raise_for_status()

        content = _decode_response_text(response).strip()

        title = ""
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        return {
            "url": url,
            "status_code": 200,
            "title": title,
            "content": content,
            "provider": "jina",
        }
    except Exception:
        logger.warning("Jina proxy webpage fetch failed", exc_info=True)
        return None


async def _fetch_webpage_async(
    url: str, timeout_seconds: int, overall_timeout: int
) -> dict[str, Any]:
    """Concurrent fetch with quality-first fallback strategy.

    Strategy:
    - If jina returns first and succeeds: use jina (best quality)
    - If direct returns first:
      - If direct failed (4xx/5xx or no content): wait for jina
      - If direct succeeded: wait for jina up to 3s, use jina if available, else direct
    """

    async def fetch_direct():
        return await asyncio.to_thread(_fetch_direct_sync, url, timeout_seconds)

    async def fetch_jina():
        return await asyncio.to_thread(_fetch_via_jina_sync, url, timeout_seconds)

    direct_task = asyncio.create_task(fetch_direct())
    jina_task = asyncio.create_task(fetch_jina())

    pending = {direct_task, jina_task}
    direct_result = None
    jina_result = None

    try:
        async with asyncio.timeout(overall_timeout):
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    if task == direct_task:
                        direct_result = task.result()
                    elif task == jina_task:
                        jina_result = task.result()

                if jina_result is not None:
                    if jina_result.get("content"):
                        if not direct_task.done():
                            direct_task.cancel()
                        return jina_result

                if direct_result is not None:
                    if direct_result.get("content"):
                        try:
                            async with asyncio.timeout(1):
                                jina_result = await jina_task
                                if jina_result and jina_result.get("content"):
                                    return jina_result
                        except asyncio.TimeoutError:
                            pass

                        return direct_result

    except asyncio.TimeoutError:
        pass
    except Exception:
        logger.warning(
            "Concurrent webpage fetch failed unexpectedly", exc_info=True
        )
    finally:
        for t in [direct_task, jina_task]:
            if not t.done():
                t.cancel()

    if jina_result and jina_result.get("content"):
        return jina_result
    if direct_result and direct_result.get("content"):
        return direct_result

    raise RuntimeError("All fetch methods failed to return content")


@tool(
    card=ToolCard(
        id="mcp_fetch_webpage",
        name="mcp_fetch_webpage",
        description=(
            "Fetch webpage text content from URL with concurrent fallback. "
            "Returns status/title/plain text content."
        ),
        properties={"truncate_length": 60000},
        input_params={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the webpage to fetch",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of content to return (500-50000, default 12000)",
                    "default": 12000,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "HTTP request timeout in seconds (3-10, default 5)",
                    "default": 5,
                },
            },
            "required": ["url"],
        },
    ),
)
async def mcp_fetch_webpage(
    url: str, max_chars: int = 12000, timeout_seconds: int = 5
) -> str:
    url = _normalize_url(url)
    if not url:
        return "[ERROR]: url cannot be empty."

    max_chars = max(500, min(max_chars, 50000))
    timeout_seconds = max(3, min(timeout_seconds, 10))
    overall_timeout = 5

    try:
        data = await _fetch_webpage_async(url, timeout_seconds, overall_timeout)
    except Exception as exc:
        return f"[ERROR]: failed to fetch webpage: {exc}"

    lines = [
        f"URL: {data.get('url', url)}",
        f"Status: {data.get('status_code', '')}",
    ]
    if data.get("title"):
        lines.append(f"Title: {data['title']}")
    if data.get("provider"):
        lines.append(f"Provider: {data['provider']}")
    lines.append("Content:")
    lines.append(_clip_text(str(data.get("content", "") or ""), max_chars) or "[empty]")
    return "\n".join(lines)
