# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Fetch and parse WeChat official-account article URLs into ``FetchedPage``."""

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

WECHAT_MP_URL_PATTERN = re.compile(
    r"^https?://(?:mp\.weixin\.qq\.com|.*?\.weixin\.qq\.com)/s\b.*",
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0


def is_wechat_article_url(url: str) -> bool:
    return bool(url and WECHAT_MP_URL_PATTERN.match(url.strip()))


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


def _extract_js_content(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    return soup.find("div", id="js_content")


def _get_text_from_soup(soup: Optional[BeautifulSoup]) -> str:
    if not soup:
        return ""
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()


async def fetch_wechat_article(
    url: str,
    doc_id: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    verify: bool | str | ssl.SSLContext = True,
    client: Optional[httpx.AsyncClient] = None,
) -> List[FetchedPage]:
    url = (url or "").strip()
    if not is_wechat_article_url(url):
        raise ValueError(f"Not a WeChat article URL: {url!r}")

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
        raise ValueError(f"WeChat article request failed: {status} for {url}") from e
    except httpx.RequestError as e:
        raise ValueError(f"WeChat article fetch failed for {url}: {e}") from e
    except Exception as e:
        raise ValueError(f"WeChat article fetch failed for {url}: {e}") from e

    soup = _parse_html(html)
    title = _extract_title(soup)
    content_node = _extract_js_content(soup)
    if not content_node:
        raise ValueError(f"Could not find article content (js_content) in page: {url}")
    text = _get_text_from_soup(content_node)
    if not text:
        raise ValueError(f"Article content is empty after parsing: {url}")

    effective_id = doc_id or url or str(uuid.uuid4())
    page = FetchedPage(
        id_=effective_id,
        text=text,
        source_url=url,
        title=title or "(无标题)",
        source_type="wechat_article",
        metadata={"source_url": url, "title": title or "(无标题)", "source_type": "wechat_article"},
    )
    logger.info("Parsed WeChat article: url=%s title=%s", url, title or "(无标题)")
    return [page]
