# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dispatch one URL to the WeChat or generic web fetcher."""

from __future__ import annotations

import ssl
import uuid
from typing import List

import httpx

from .models import FetchedPage
from .wechat_article import fetch_wechat_article, is_wechat_article_url
from .web_page import fetch_web_page


async def fetch_pages_from_url(
    url: str,
    *,
    timeout: float | None = None,
    user_agent: str | None = None,
    verify: bool | str | ssl.SSLContext = True,
    client: httpx.AsyncClient | None = None,
) -> List[FetchedPage]:
    """Fetch one URL and return a list of ``FetchedPage`` (typically one element).

    Optional ``timeout`` and ``user_agent`` override each backend module default.
    """
    u = (url or "").strip()
    doc_id = str(uuid.uuid4())
    kw: dict = {"doc_id": doc_id, "verify": verify, "client": client}
    if timeout is not None:
        kw["timeout"] = timeout
    if user_agent is not None:
        kw["user_agent"] = user_agent

    if is_wechat_article_url(u):
        return await fetch_wechat_article(u, **kw)
    return await fetch_web_page(u, **kw)
