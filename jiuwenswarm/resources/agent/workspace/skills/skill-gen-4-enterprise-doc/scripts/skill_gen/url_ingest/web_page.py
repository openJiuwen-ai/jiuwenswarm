# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Fetch and parse generic HTTP(S) page URLs into ``FetchedPage``."""

from __future__ import annotations

import logging
import re
import ssl
import uuid
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .models import FetchedPage

logger = logging.getLogger(__name__)
from .wechat_article import is_wechat_article_url

HTTP_URL_PATTERN = re.compile(r"^https?://\S+", re.IGNORECASE)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0

MAIN_CONTENT_SELECTORS = [
    "article",
    "main",
    '[role="main"]',
    ".article-body",
    ".post-content",
    ".content",
    ".entry-content",
    ".post-body",
    "#content",
    ".main-content",
]


def _parse_html(html: str) -> BeautifulSoup:
    try:
        import lxml  # noqa: F401
        return BeautifulSoup(html, "lxml")
    except ImportError:
        return BeautifulSoup(html, "html.parser")


def _extract_title(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        return (meta["content"] or "").strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        return title_tag.string.strip()
    return ""


def _text_length(node: BeautifulSoup) -> int:
    total = 0
    for elem in node.find_all(string=True):
        if elem.parent and elem.parent.name not in ("script", "style"):
            total += len(elem.strip())
    return total


def _find_main_content(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    for sel in MAIN_CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node and _text_length(node) > 100:
            return node
    body = soup.find("body")
    if body:
        for tag in body.find_all(["article", "main", "div", "section"]):
            if _text_length(tag) > 200:
                return tag
    return body


def _get_text_from_soup(soup: Optional[BeautifulSoup]) -> str:
    if not soup:
        return ""
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()


async def fetch_web_page(
    url: str,
    doc_id: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    verify: bool | str | ssl.SSLContext = True,
    client: Optional[httpx.AsyncClient] = None,
) -> List[FetchedPage]:
    url = (url or "").strip()
    if not url or not HTTP_URL_PATTERN.match(url):
        raise ValueError(f"Not a valid HTTP URL: {url!r}")
    if is_wechat_article_url(url):
        raise ValueError("WeChat article URLs must use the WeChat fetcher")

    request_headers = {"User-Agent": user_agent}

    async def _do_get(c: httpx.AsyncClient, *, headers_for_request: Optional[dict] = None) -> str:
        response = await c.get(url, headers=headers_for_request)
        response.raise_for_status()
        return response.text

    try:
        if client is not None:
            html = await _do_get(client, headers_for_request=request_headers)
        else:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout),
                headers=request_headers,
            ) as http_client:
                html = await _do_get(http_client, headers_for_request=None)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "?"
        raise ValueError(f"Web page request failed: {status} for {url}") from e
    except httpx.RequestError as e:
        raise ValueError(f"Web page fetch failed for {url}: {e}") from e
    except Exception as e:
        raise ValueError(f"Web page fetch failed for {url}: {e}") from e

    soup = _parse_html(html)
    title = _extract_title(soup)
    content_node = _find_main_content(soup)
    if not content_node:
        raise ValueError(f"Could not find main content in page: {url}")
    text = _get_text_from_soup(content_node)
    if not text or len(text) < 50:
        raise ValueError(f"Article content too short or empty after parsing: {url}")

    effective_id = doc_id or url or str(uuid.uuid4())
    page = FetchedPage(
        id_=effective_id,
        text=text,
        source_url=url,
        title=title or "(无标题)",
        source_type="web_page",
        metadata={"source_url": url, "title": title or "(无标题)", "source_type": "web_page"},
    )
    logger.info("Parsed web page: url=%s title=%s", url, title or "(无标题)")
    return [page]


def supports_generic_web_url(url: str) -> bool:
    return bool(url and HTTP_URL_PATTERN.match(url.strip()) and not is_wechat_article_url(url))
