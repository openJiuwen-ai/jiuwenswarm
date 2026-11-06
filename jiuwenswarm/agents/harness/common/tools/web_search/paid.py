# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Paid search providers (petal, tavily, perplexity, serper, jina, bocha)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.tools.web_search.http_client import http_request
from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import parse_update_time
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.local_env_config import read_env, read_env_live

logger = logging.getLogger(__name__)
baseline = logging.getLogger("jiuwenswarm.agents.harness.common.tools.web_search.baseline")

_PETAL_MAX_TITLE_LEN = 2000
_PETAL_MAX_URL_LEN = 2048
_PETAL_MAX_SUMMARY_LEN = 4000
_PETAL_MAX_CONTENT_LEN = 20000
_TAVILY_DEFAULT_API_URL = "https://api.tavily.com"
_TAVILY_MAX_CONTENT_LEN = 4000


def _resolve_petal_search_url() -> str:
    petal_url = read_env_live("PETAL_SEARCH_URL", "").strip()
    if not petal_url:
        raise ValueError("PETAL_SEARCH_URL is not set")
    return petal_url


def _load_llm_default_headers() -> dict[str, str]:
    from jiuwenswarm.common.local_env_config import parse_default_headers
    raw = read_env_live("PETAL_SEARCH_HEADERS", "")
    header_map = parse_default_headers(raw)
    if not header_map:
        raise ValueError("PETAL_SEARCH_HEADERS is not set")
    return header_map


_PETAL_IMAGE_KEYS = (
    "images",
    "image_urls",
    "image",
    "pic_url",
    "picUrl",
    "thumbnail",
    "thumb",
    "cover",
    "img",
)
_MAX_IMAGES_PER_ITEM = 5


def _collect_image_urls(value: Any, out: list[str]) -> None:
    """Recursively collect http(s) image URLs from a loosely-typed payload value."""
    if isinstance(value, str):
        url = value.strip()
        if url.startswith(("http://", "https://")):
            out.append(url)
        return
    if isinstance(value, dict):
        for key in ("url", "image_url", "contentUrl", "src", "link", "thumbnail", "thumbnailUrl"):
            nested = value.get(key)
            if isinstance(nested, str):
                _collect_image_urls(nested, out)
        return
    if isinstance(value, list):
        for entry in value:
            _collect_image_urls(entry, out)


def _dedupe_image_urls(found: list[str], limit: int = _MAX_IMAGES_PER_ITEM) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= limit:
            break
    return deduped


def _extract_item_image_urls(item: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Extract image URLs from a web page item using provider-specific field spellings."""
    found: list[str] = []
    for key in keys:
        if key in item:
            _collect_image_urls(item.get(key), found)
    return _dedupe_image_urls(found)


def _petal_extract_image_urls(item: dict[str, Any]) -> list[str]:
    """Collect image URLs from a petal web page item.

    Petal's schema is not documented in-repo, so several field spellings are
    tolerated. When none is present the caller still has HTML extraction as a
    fallback; this is a cheap shortcut, not the only source.
    """
    return _extract_item_image_urls(item, _PETAL_IMAGE_KEYS)


def _petal_normalize_web_page_item(item: dict[str, Any]) -> dict[str, Any]:
    raw_url = item.get("url") or item.get("link") or item.get("source_url") or ""
    title = (item.get("title") or item.get("name") or "").strip()
    summary = (item.get("summary") or "").strip()
    content = (item.get("content") or item.get("text") or "").strip()
    update_time_raw = (
        item.get("update_time") or item.get("publish_time") or item.get("date")
    )
    return {
        "title": title[:_PETAL_MAX_TITLE_LEN],
        "url": str(raw_url).strip()[:_PETAL_MAX_URL_LEN],
        "summary": summary[:_PETAL_MAX_SUMMARY_LEN],
        "content": content[:_PETAL_MAX_CONTENT_LEN],
        "update_time": parse_update_time(update_time_raw),
        "images": _petal_extract_image_urls(item),
    }


def _petal_format_answer_from_records(records: list[dict[str, str]]) -> str:
    lines: list[str] = []
    n = 0
    for rec in records:
        title = (rec.get("title") or "").strip()
        url = (rec.get("url") or "").strip()
        # 优先用完整 content；content 缺失时回退 summary（petal 的 summary 可能被截断）
        summary = (rec.get("content") or "").strip() or (rec.get("summary") or "").strip()
        if not title and not url and not summary:
            continue
        n += 1
        display_title = title if title else "(无标题)"
        lines.append(f"{n}. {display_title}")
        if url:
            lines.append(f"   URL: {url}")
        if summary:
            label = "Content" if (rec.get("content") or "").strip() else "Summary"
            if len(summary) > _PETAL_MAX_SUMMARY_LEN:
                summary = summary[:_PETAL_MAX_SUMMARY_LEN] + " ...(body truncated)"
            lines.append(f"   {label}: {summary}")
    return "\n".join(lines)


def petal_search_sync(query: str, max_results: int, timeout_seconds: int) -> dict[str, Any]:
    _start = time.perf_counter()
    search_url = _resolve_petal_search_url()
    header_map = _load_llm_default_headers()
    headers = {**header_map, "Content-Type": "application/json"}
    payload = {"query": query, "content": True}

    response = http_request(
        "POST",
        search_url,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    web_pages = data.get("web_pages", [])

    records: list[dict[str, Any]] = []
    if isinstance(web_pages, list):
        for raw in web_pages[:max_results]:
            if not isinstance(raw, dict):
                continue
            records.append(_petal_normalize_web_page_item(raw))

    answer = _petal_format_answer_from_records(records)
    urls = [r["url"] for r in records if r.get("url")][:max_results]
    cost_ms = int((time.perf_counter() - _start) * 1000)
    baseline.info(
        "BASELINE petal_search query=%r urls=%d cost_ms=%d",
        query,
        len(urls),
        cost_ms,
    )
    return {
        "provider": "petal",
        "answer": answer,
        "urls": urls,
        "records": records,
    }


def enable_petal_search() -> bool:
    return diagnose_petal_search() == "ok"


def diagnose_petal_search() -> str:
    """Return ``ok`` when Petal is usable, else a short reason for server logs."""
    try:
        if not bool(get_config().get("enable_petal_web_search", False)):
            return "enable_petal_web_search=false"
        _resolve_petal_search_url()
        _load_llm_default_headers()
        return "ok"
    except Exception as exc:
        return str(exc)


def _parse_perplexity_citations(data: dict[str, Any]) -> list[str]:
    for key in ("citations", "search_results", "web_search_results", "sources"):
        entries = data.get(key)
        if not isinstance(entries, list):
            continue
        urls: list[str] = []
        for item in entries:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                maybe_url = (
                    item.get("url") or item.get("link") or item.get("source_url")
                )
                if maybe_url:
                    urls.append(str(maybe_url))
        if urls:
            return urls
    return []


def perplexity_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    perplexity_key = read_env("PERPLEXITY_API_KEY", "")
    if not perplexity_key:
        raise ValueError("PERPLEXITY_API_KEY is not set")

    payload = {
        "model": read_env("PPLX_MODEL", "sonar-pro"),
        "messages": [
            {
                "role": "system",
                "content": "Provide concise answer and include citations.",
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 1024,
        "temperature": 0.2,
        "stream": False,
    }
    response = http_request(
        "POST",
        read_env("PPLX_API_URL", "https://api.perplexity.ai/chat/completions"),
        headers={
            "Authorization": f"Bearer {perplexity_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()

    answer = ""
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        answer = choices[0].get("message", {}).get("content", "")

    return {
        "provider": "perplexity",
        "answer": (answer or "").strip(),
        "urls": _parse_perplexity_citations(data)[:max_results],
    }


def serper_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    serper_key = read_env("SERPER_API_KEY", "")
    if not serper_key:
        raise ValueError("SERPER_API_KEY is not set")

    response = http_request(
        "POST",
        "https://google.serper.dev/search",
        headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
        json={"q": query, "num": max_results},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    urls: list[str] = []
    organic = data.get("organic", [])
    if isinstance(organic, list):
        for item in organic[:max_results]:
            if isinstance(item, dict) and item.get("link"):
                urls.append(str(item["link"]))
    return {"provider": "serper", "answer": "", "urls": urls}


def jina_search_sync(query: str, timeout_seconds: int) -> dict[str, Any]:
    jina_key = read_env("JINA_API_KEY", "")
    if not jina_key:
        raise ValueError("JINA_API_KEY is not set")

    payload = {
        "model": "jina-deepsearch-v1",
        "messages": [{"role": "user", "content": query}],
        "stream": False,
        "reasoning_effort": "low",
    }
    response = http_request(
        "POST",
        "https://deepsearch.jina.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {jina_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()

    answer = ""
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        answer = choices[0].get("message", {}).get("content", "")
    urls = re.findall(r"https?://[^\s)\]>\"']+", answer or "")
    return {"provider": "jina", "answer": (answer or "").strip(), "urls": urls}


_BOCHA_MAX_TITLE_LEN = 2000
_BOCHA_MAX_URL_LEN = 2048
_BOCHA_MAX_SUMMARY_LEN = 4000
_BOCHA_MAX_CONTENT_LEN = 20000
# Bocha 兼容 Bing Search API：网页项内的图片字段 + 顶层 images.value 图片答案
_BOCHA_IMAGE_KEYS = (
    "images",
    "imageUrl",
    "image_url",
    "image",
    "thumbnailUrl",
    "thumbnail",
    "primaryImageOfPage",
    "picUrl",
)


def _bocha_payload_roots(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return both the inner ``data`` object and the envelope, inner first.

    网关/代理包装层可能把结果放在内层，也可能同时保留顶层副本；只锁定一层
    会让另一种形态整体“无结果”，因此按内层 → 顶层的顺序依次兜底。
    """
    inner = data.get("data")
    if isinstance(inner, dict) and inner is not data:
        return [inner, data]
    return [data]


def _bocha_web_page_items(root: dict[str, Any]) -> list[dict[str, Any]]:
    web_pages = root.get("webPages")
    candidates: list[Any] = []
    if isinstance(web_pages, dict):
        candidates.append(web_pages.get("value"))
    else:
        candidates.append(web_pages)
    candidates.append(root.get("results"))
    for container in candidates:
        if not isinstance(container, list):
            continue
        items = [item for item in container if isinstance(item, dict)]
        if items:
            return items
    return []


def _bocha_collect_page_items(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First root that yields web pages wins; results are not merged across layers."""
    for root in roots:
        items = _bocha_web_page_items(root)
        if items:
            return items
    return []


def _bocha_normalize_web_page_item(item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url") or item.get("link") or "").strip()[:_BOCHA_MAX_URL_LEN]
    title = str(item.get("name") or item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    snippet = str(item.get("snippet") or item.get("content") or "").strip()
    return {
        "url": url,
        "title": title[:_BOCHA_MAX_TITLE_LEN],
        "summary": summary[:_BOCHA_MAX_SUMMARY_LEN],
        "content": snippet[:_BOCHA_MAX_CONTENT_LEN],
        "images": _extract_item_image_urls(item, _BOCHA_IMAGE_KEYS),
    }


def _bocha_collect_answer_images(roots: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Collect the ``images`` answer from every payload layer, deduplicated by URL."""
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        node = root.get("images")
        entries = node.get("value") if isinstance(node, dict) else node
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                raw_url, host_page, name = entry, "", ""
            elif isinstance(entry, dict):
                raw_url = (
                    entry.get("contentUrl")
                    or entry.get("url")
                    or entry.get("imageUrl")
                    or entry.get("thumbnailUrl")
                    or ""
                )
                host_page = (
                    entry.get("hostPageUrl")
                    or entry.get("hostPageDisplayUrl")
                    or entry.get("pageUrl")
                    or ""
                )
                name = entry.get("name") or entry.get("title") or ""
            else:
                continue
            url = str(raw_url).strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            collected.append(
                {"url": url, "host_page": str(host_page).strip(), "name": str(name).strip()}
            )
    return collected


def _page_key(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{(parsed.netloc or '').lower()}{(parsed.path or '/').rstrip('/')}"


def _bocha_attach_images(
    records: list[dict[str, Any]], images: list[dict[str, str]]
) -> None:
    """Attach the images answer to the page it was crawled from."""
    by_page: dict[str, dict[str, Any]] = {}
    by_host: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.get("url"):
            continue
        by_page.setdefault(_page_key(record["url"]), record)
        host = (urlparse(record["url"]).netloc or "").lower()
        if host:
            by_host.setdefault(host, record)

    for image in images:
        host_page = image.get("host_page", "")
        target = by_page.get(_page_key(host_page)) if host_page else None
        if target is None and host_page:
            target = by_host.get((urlparse(host_page).netloc or "").lower())
        if target is None:
            continue
        bucket = target.setdefault("images", [])
        if image["url"] in bucket or len(bucket) >= _MAX_IMAGES_PER_ITEM:
            continue
        bucket.append(image["url"])


def bocha_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    bocha_key = read_env("BOCHA_API_KEY", "")
    if not bocha_key:
        raise ValueError("BOCHA_API_KEY is not set")

    response = http_request(
        "POST",
        read_env("BOCHA_API_URL", "https://api.bocha.cn/v1/web-search"),
        headers={"Authorization": f"Bearer {bocha_key}", "Content-Type": "application/json"},
        json={"query": query, "summary": True, "count": max_results},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    roots = _bocha_payload_roots(data)

    records = [
        _bocha_normalize_web_page_item(item)
        for item in _bocha_collect_page_items(roots)[:max_results]
    ]
    records = [record for record in records if record["url"]]
    _bocha_attach_images(records, _bocha_collect_answer_images(roots))

    answer = ""
    for root in roots:
        for value in (root.get("summary"), root.get("answer")):
            if isinstance(value, str) and value.strip():
                answer = value.strip()
                break
        if answer:
            break

    urls = [record["url"] for record in records]
    return {
        "provider": "bocha",
        "answer": answer,
        "urls": urls[:max_results],
        "records": records[:max_results],
    }


def _resolve_tavily_api_key() -> str:
    return read_env("TAVILY_API_KEY", "").strip()


def _resolve_tavily_api_url() -> str:
    raw = (
        read_env("TAVILY_API_URL", "").strip()
        or _TAVILY_DEFAULT_API_URL
    )
    return raw.rstrip("/")


def _tavily_format_answer_from_results(
    results: list[dict[str, Any]], summary_answer: str
) -> str:
    lines: list[str] = []
    summary = (summary_answer or "").strip()
    if summary:
        lines.append("Summary:")
        lines.append(summary)
        lines.append("")
    n = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()[:_TAVILY_MAX_CONTENT_LEN]
        if not title and not url and not content:
            continue
        n += 1
        display_title = title if title else "(无标题)"
        lines.append(f"{n}. {display_title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   Content: {content}")
    return "\n".join(lines).strip()


def tavily_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    api_key = _resolve_tavily_api_key()
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not set")

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": True,
    }
    response = http_request(
        "POST",
        f"{_resolve_tavily_api_url()}/search",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout_seconds,
    )
    payload.pop("api_key", None)
    response.raise_for_status()
    data = response.json()

    results_raw = data.get("results", [])
    results: list[dict[str, Any]] = []
    if isinstance(results_raw, list):
        for item in results_raw[:max_results]:
            if isinstance(item, dict):
                results.append(item)

    summary_answer = (data.get("answer") or "").strip()

    answer = _tavily_format_answer_from_results(results, summary_answer)
    urls: list[str] = []
    for item in results:
        maybe_url = item.get("url")
        if maybe_url:
            urls.append(str(maybe_url))
    return {"provider": "tavily", "answer": answer, "urls": urls[:max_results]}
