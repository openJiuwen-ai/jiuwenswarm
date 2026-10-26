# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP(S) and WeChat URL fetch helpers for SOP / skill draft input (httpx + bs4)."""

from __future__ import annotations

import ssl
from typing import List

import httpx

from .models import FetchedPage
from .router import fetch_pages_from_url
from .url_safety import check_url_allowed_for_fetch, is_likely_public_http_url
from .wechat_article import is_wechat_article_url
from .web_page import supports_generic_web_url


def fetch_url_as_plaintext(
    pages: list[FetchedPage],
    *,
    join_sep: str = "\n\n---\n\n",
) -> str:
    """Turn fetched pages into one document string for SOP / skill draft input."""
    parts: list[str] = []
    for p in pages:
        header_lines = [f"Source: {p.source_url}"]
        if p.title:
            header_lines.insert(0, f"Title: {p.title}")
        parts.append("\n".join(header_lines) + "\n\n" + p.text.strip())
    return join_sep.join(parts)


async def validate_fetch_and_flatten(
    url: str,
    *,
    timeout: float | None = None,
    user_agent: str | None = None,
    verify: bool | str | ssl.SSLContext = True,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, List[FetchedPage]]:
    """Run SSRF checks, fetch the URL, return ``(flattened_text, pages)``."""
    check_url_allowed_for_fetch(url)
    pages = await fetch_pages_from_url(
        url,
        timeout=timeout,
        user_agent=user_agent,
        verify=verify,
        client=client,
    )
    return fetch_url_as_plaintext(pages), pages


__all__ = [
    "FetchedPage",
    "check_url_allowed_for_fetch",
    "fetch_pages_from_url",
    "fetch_url_as_plaintext",
    "is_likely_public_http_url",
    "is_wechat_article_url",
    "supports_generic_web_url",
    "validate_fetch_and_flatten",
]
