# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Petal Search：通过 LLM 同源的 OpenAI 兼容网关调用 ``/v1/ai-tools/web-search``。

与对话模型共用 ``API_BASE``（或 ``OPENAI_BASE_URL`` / ``OPENAI_API_BASE``）及 ``default_headers``（JSON），
由 ``enable_petal_web_search`` 配置项与环境共同决定是否可用。实现独立于 ``search_tools``。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import requests
from jiuwenclaw.utils import build_default_headers
from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.agentserver.tools.ssl_config import get_requests_verify
from jiuwenclaw.config import get_config

_PETAL_MAX_TITLE_LEN = 2000
_PETAL_MAX_URL_LEN = 2048
_PETAL_MAX_CONTENT_LEN = 8000


def _resolve_petal_search_url() -> str:
    """从 LLM 的 API_BASE 推导 Petal Search URL：去掉末尾 ``/v2``，再拼 ``/v1/ai-tools/web-search``。"""
    api_base = (
        os.environ.get("API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or ""
    ).strip()
    if not api_base:
        raise ValueError(
            "API_BASE is not set (use the same OpenAI-compatible base URL as the LLM)"
        )
    trimmed = api_base.rstrip("/")
    if trimmed.lower().endswith("/v2"):
        trimmed = trimmed[:-3]
    trimmed = trimmed.rstrip("/")
    return f"{trimmed}/v1/ai-tools/web-search"


def _load_llm_default_headers() -> dict[str, str]:
    """与 LLM 客户端一致的 JSON 头（如 Authorization）；环境变量名 ``default_headers``。"""
    headers = build_default_headers()
    if not headers:
        raise ValueError("default_headers is not set (use the same JSON headers as the LLM, e.g. Authorization)")
    if not isinstance(headers, dict):
        raise ValueError("default_headers must be a JSON object")
    return {str(k): str(v) for k, v in headers.items() if v is not None}


def _petal_http_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", get_requests_verify())
    method_up = method.upper()
    try:
        if method_up == "GET":
            return requests.get(url, **kwargs)
        if method_up == "POST":
            return requests.post(url, **kwargs)
        return requests.request(method_up, url, **kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method_up, url, **kwargs)


def _petal_normalize_web_page_item(item: dict[str, Any]) -> dict[str, str]:
    raw_url = item.get("url") or item.get("link") or item.get("source_url") or ""
    title = (item.get("title") or item.get("name") or "").strip()
    content = (
        item.get("content")
        or item.get("snippet")
        or item.get("description")
        or item.get("summary")
        or ""
    )
    content = str(content).strip()
    return {
        "title": title[:_PETAL_MAX_TITLE_LEN],
        "url": str(raw_url).strip()[:_PETAL_MAX_URL_LEN],
        "content": content[:_PETAL_MAX_CONTENT_LEN],
    }


def _petal_format_answer_from_records(records: list[dict[str, str]]) -> str:
    lines: list[str] = []
    n = 0
    for rec in records:
        title = (rec.get("title") or "").strip()
        url = (rec.get("url") or "").strip()
        content = (rec.get("content") or "").strip()
        if not title and not url and not content:
            continue
        n += 1
        display_title = title if title else "(无标题)"
        lines.append(f"{n}. {display_title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   Content: {content}")
    return "\n".join(lines)


def petal_search_sync(query: str, max_results: int, timeout_seconds: int) -> dict[str, Any]:
    """Petal Search API：POST JSON ``query`` + ``content``；解析 ``web_pages[]``。"""
    search_url = _resolve_petal_search_url()
    header_map = _load_llm_default_headers()
    headers = {**header_map, "Content-Type": "application/json"}
    payload = {"query": query, "content": True}

    response = _petal_http_request(
        "POST",
        search_url,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    web_pages = data.get("web_pages", [])

    records: list[dict[str, str]] = []
    if isinstance(web_pages, list):
        for raw in web_pages[:max_results]:
            if not isinstance(raw, dict):
                continue
            records.append(_petal_normalize_web_page_item(raw))

    answer = _petal_format_answer_from_records(records)
    urls = [r["url"] for r in records if r.get("url")][:max_results]
    return {"provider": "petal", "answer": answer, "urls": urls}


def enable_petal_search() -> bool:
    """配置开启且 ``API_BASE`` + ``default_headers`` 可用时为 True。"""
    try:
        if not bool(get_config().get("enable_petal_web_search", False)):
            return False
        _resolve_petal_search_url()
        _load_llm_default_headers()
        return True
    except Exception:
        return False


@tool(
    name="mcp_petal_search",
    description=(
        "Preferred web search tool when available. Use this before mcp_free_search for web searches. "
        "Petal web search via the same OpenAI-compatible API_BASE as the LLM "
        "(POST .../v1/ai-tools/web-search). Requires enable_petal_web_search in config and default_headers env."
    ),
)
async def mcp_petal_search(
    query: str,
    max_results: int = 8,
    timeout_seconds: int = 45,
) -> str:
    query = (query or "").strip()
    if not query:
        return "[ERROR]: query cannot be empty."

    if not enable_petal_search():
        return (
            "[ERROR]: Petal search is disabled or misconfigured. "
            "Set enable_petal_web_search: true in config, and API_BASE plus default_headers (JSON) in the environment."
        )

    max_results = max(1, min(max_results, 20))
    timeout_seconds = max(10, min(timeout_seconds, 120))

    try:
        result = await asyncio.to_thread(petal_search_sync, query, max_results, timeout_seconds)
    except Exception as exc:
        return f"[ERROR]: petal search failed: {exc}"

    answer = str(result.get("answer", "") or "").strip()
    urls = [str(u) for u in (result.get("urls", []) or []) if u][:max_results]
    lines = ["Petal search results for: " + query]
    if answer:
        lines.append(answer)
    if urls and not answer:
        lines.append("URLs:")
        for idx, url in enumerate(urls, 1):
            lines.append(f"{idx}. {url}")
    if not answer and not urls:
        lines.append("No usable result payload.")
    return "\n".join(lines)


__all__ = [
    "enable_petal_search",
    "mcp_petal_search",
    "petal_search_sync",
]
