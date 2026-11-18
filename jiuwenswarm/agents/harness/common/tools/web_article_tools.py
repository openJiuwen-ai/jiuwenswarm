# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Smart web article extraction and noise-reduction tool.

Uses trafilatura to extract clean article content, titles, authors, dates, and tables
formatted as Markdown. Automatically strips ads, navigation bars, footers, and tracking
scripts, reducing token consumption compared to raw HTML scraping.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import (
    _check_fetch_url,
    decode_response_text,
    fetch_response,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 40_000
_MAX_CHARS_CEILING = 150_000
DEFAULT_TIMEOUT_SECONDS = 15
_MAX_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ReadArticleRequest:
    """Normalized request parameters for reading a web article."""

    url: str
    max_chars: int = DEFAULT_MAX_CHARS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    include_comments: bool = False


def _format_article_output(
    target_url: str, extracted_md: str, metadata: Any, max_chars: int
) -> str:
    """Format extracted article markdown with metadata header and length cap."""
    lines: list[str] = [f"**Source URL: {target_url}**"]
    if metadata:
        if metadata.title:
            lines.append(f"**Title: {metadata.title}**")
        meta_items: list[str] = []
        if metadata.author:
            meta_items.append(f"Author: {metadata.author}")
        if metadata.date:
            meta_items.append(f"Date: {metadata.date}")
        if meta_items:
            lines.append(" | ".join(meta_items))
    lines.append("\n---\n")
    lines.append(extracted_md.strip())
    full_output = "\n".join(lines)
    if len(full_output) > max_chars:
        full_output = (
            full_output[:max_chars]
            + f"\n\n[Article truncated at {max_chars} characters. "
            "Raise `max_chars` to read more.]"
        )
    return full_output


def _read_web_article_sync(req: ReadArticleRequest) -> str:
    """Fetch and extract clean article markdown using trafilatura."""
    import trafilatura  # pylint: disable=import-outside-toplevel

    target_url = req.url.strip()
    if not target_url:
        raise ValueError("URL cannot be empty")
    if not target_url.startswith(("http://", "https://")):
        raise ValueError(f"URL must start with http:// or https://, got: {target_url}")

    try:
        _check_fetch_url(target_url)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"URL failed safety check: {exc}") from exc

    try:
        response = fetch_response(
            target_url, timeout_seconds=req.timeout_seconds, public_https=True
        )
        response.raise_for_status()
        html_content = decode_response_text(response)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Failed to fetch URL '{target_url}': {exc}") from exc

    if not html_content or not html_content.strip():
        return f"[Web page at '{target_url}' returned empty body]"

    try:
        extracted_md = trafilatura.extract(
            html_content,
            url=target_url,
            output_format="markdown",
            include_comments=req.include_comments,
            include_tables=True,
            include_images=True,
            favor_precision=True,
        )
        metadata = trafilatura.extract_metadata(html_content, default_url=target_url)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Article extraction failed: {exc}") from exc

    if not extracted_md or not extracted_md.strip():
        return (
            f"[Notice: Trafilatura could not extract article text from '{target_url}'. "
            "The page may be client-side JavaScript rendered or a non-article landing page. "
            "Try using `mcp_fetch_webpage` to inspect the raw HTML or a browser tool.]"
        )
    return _format_article_output(target_url, extracted_md, metadata, req.max_chars)


@tool(
    name="read_web_article",
    description=(
        "Extract clean, distraction-free article text, tables, and metadata from a web URL. "
        "Automatically strips advertisements, navigation menus, and footers to save tokens. "
        "Use this tool when a webpage or news/blog/documentation URL needs reading. "
        "Input: url (http/https link), optional max_chars (1000-150000, default 40000), "
        "optional timeout_seconds (1-60, default 15), optional include_comments (default False)."
    ),
)
async def read_web_article(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    include_comments: bool = False,
) -> str:
    """Read clean distraction-free Markdown text and tables from a web article URL."""
    try:
        try:
            safe_max = int(max_chars)
        except (TypeError, ValueError):
            safe_max = DEFAULT_MAX_CHARS
        safe_max = max(1_000, min(safe_max, _MAX_CHARS_CEILING))

        try:
            safe_timeout = int(timeout_seconds)
        except (TypeError, ValueError):
            safe_timeout = DEFAULT_TIMEOUT_SECONDS
        safe_timeout = max(1, min(safe_timeout, _MAX_TIMEOUT_SECONDS))

        req = ReadArticleRequest(
            url=str(url or "").strip(),
            max_chars=safe_max,
            timeout_seconds=safe_timeout,
            include_comments=bool(include_comments),
        )
        logger.info(
            "[read_web_article] url=%s max_chars=%s timeout=%s",
            req.url,
            req.max_chars,
            req.timeout_seconds,
        )
        return await asyncio.to_thread(_read_web_article_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: read_web_article failed: {exc}"
