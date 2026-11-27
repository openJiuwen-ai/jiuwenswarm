# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Search tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.agentserver.tools.ssl_config import get_requests_verify
from jiuwenclaw.utils import build_default_headers

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    """Try normal request first; retry without env proxies on ProxyError."""
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


def _decode_bing_redirect(url: str) -> str:
    parsed = urlparse(url)
    if "bing.com" not in parsed.netloc.lower() or parsed.path != "/ck/a":
        return url

    query = parse_qs(parsed.query)
    values = query.get("u")
    if not values:
        return url
    encoded = values[0]
    if not encoded:
        return url

    if encoded.startswith("a1"):
        payload = encoded[2:]
        padding = "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(
                (payload + padding).encode("utf-8")
            ).decode("utf-8", errors="ignore")
            if decoded.startswith(("http://", "https://")):
                return decoded
        except Exception:
            logger.warning("Failed to decode Bing redirect URL", exc_info=True)
            return url
    elif encoded.startswith(("http://", "https://")):
        return encoded

    return url


def _is_ddg_challenge_page(status_code: int, html: str) -> bool:
    if status_code in {202, 418, 429, 503}:
        return True
    text = (html or "").lower()
    markers = [
        "/anomaly.js",
        "challenge-form",
        "duckduckgo.com/anomaly.js",
    ]
    return any(marker in text for marker in markers)


def _search_duckduckgo_sync(
    query: str, max_results: int, timeout_seconds: int
) -> list[dict[str, str]]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    response = _http_request(
        "GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds
    )
    if _is_ddg_challenge_page(response.status_code, response.text):
        raise RuntimeError("DuckDuckGo anti-bot challenge page returned")
    if response.status_code != 200:
        raise RuntimeError(
            f"DuckDuckGo returned non-200 status: {response.status_code}"
        )
    response.raise_for_status()
    html = response.text

    links = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    snippets = re.findall(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(.*?)</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    rows: list[dict[str, str]] = []
    for index, (href, title_raw) in enumerate(links[:max_results]):
        snippet_raw = ""
        if index < len(snippets):
            snippet_raw = snippets[index][0] or snippets[index][1] or ""
        rows.append(
            {
                "title": _strip_tags(title_raw) or f"Result {index + 1}",
                "url": _decode_ddg_redirect(href),
                "snippet": _strip_tags(snippet_raw),
            }
        )
    return rows


def _search_duckduckgo_via_jina_sync(
    query: str, max_results: int, timeout_seconds: int
) -> list[dict[str, str]]:
    url = f"https://r.jina.ai/http://duckduckgo.com/html/?q={quote_plus(query)}"
    response = _http_request(
        "GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds
    )
    response.raise_for_status()
    text = response.text or ""

    # Parse markdown links rendered by r.jina.ai.
    matches = re.findall(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", text, flags=re.IGNORECASE
    )

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for title_raw, href in matches:
        title = _strip_tags(title_raw)
        if not title or title.startswith("Image "):
            continue
        decoded = _decode_ddg_redirect(href)
        parsed = urlparse(decoded)
        if not parsed.scheme.startswith("http"):
            continue
        # Drop DuckDuckGo navigation/self links.
        if "duckduckgo.com" in parsed.netloc.lower():
            continue
        if decoded in seen:
            continue
        seen.add(decoded)
        rows.append({"title": title, "url": decoded, "snippet": ""})
        if len(rows) >= max_results:
            break
    return rows


def _search_bing_sync(
    query: str, max_results: int, timeout_seconds: int
) -> list[dict[str, str]]:
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    response = _http_request(
        "GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds
    )
    response.raise_for_status()
    html = response.text

    blocks = re.findall(
        r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for block in blocks:
        title_match = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue
        href_raw = unescape(title_match.group(1))
        href = _decode_bing_redirect(href_raw)
        title = _strip_tags(title_match.group(2))
        if not href or href in seen:
            continue
        seen.add(href)
        snippet_match = re.search(
            r"<p>(.*?)</p>", block, flags=re.IGNORECASE | re.DOTALL
        )
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        rows.append(
            {
                "title": title or f"Result {len(rows) + 1}",
                "url": href,
                "snippet": snippet,
            }
        )
        if len(rows) >= max_results:
            break

    return rows


def _search_free_sync(
    query: str, max_results: int, timeout_seconds: int
) -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    engines = [
        ("duckduckgo", _search_duckduckgo_sync),
        ("duckduckgo-jina", _search_duckduckgo_via_jina_sync),
        ("bing", _search_bing_sync),
    ]
    for engine_name, runner in engines:
        try:
            rows = runner(query, max_results, timeout_seconds)
        except Exception as exc:
            errors.append(f"{engine_name}: {exc}")
            continue
        if rows:
            return engine_name, rows
        errors.append(f"{engine_name}: empty result")
    raise RuntimeError(" | ".join(errors))


async def _search_free_async(
    query: str, max_results: int, timeout_seconds: int, overall_timeout: int
) -> tuple[str, list[dict[str, str]]]:
    """Concurrent search with quality-first fallback strategy.

    Quality ranking: DDG = DDG-jina > Bing

    Strategy:
    - If DDG/DDG-jina returns first and succeeds: use it (best quality)
    - If Bing returns first and succeeds: wait for DDG/DDG-jina up to 2s
    """
    engines = [
        ("duckduckgo", _search_duckduckgo_sync),
        ("duckduckgo-jina", _search_duckduckgo_via_jina_sync),
        ("bing", _search_bing_sync),
    ]

    async def run_engine(
        engine_name: str, runner
    ) -> tuple[str, list[dict[str, str]]] | None:
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(runner, query, max_results, timeout_seconds),
                timeout=timeout_seconds,
            )
            if rows:
                return engine_name, rows
            return None
        except Exception:
            logger.warning(
                "Free search engine failed: %s", engine_name, exc_info=True
            )
            return None

    tasks = {asyncio.create_task(run_engine(name, fn)): name for name, fn in engines}
    results: dict[str, tuple[str, list[dict[str, str]]]] = {}

    try:
        async with asyncio.timeout(overall_timeout):
            pending = set(tasks.keys())
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    result = task.result()
                    if result:
                        engine_name, rows = result
                        results[engine_name] = result

                        if engine_name in ("duckduckgo", "duckduckgo-jina"):
                            for t in pending:
                                t.cancel()
                            return engine_name, rows

                if (
                    "bing" in results
                    and "duckduckgo" not in results
                    and "duckduckgo-jina" not in results
                ):
                    try:
                        async with asyncio.timeout(1):
                            while pending:
                                done2, pending = await asyncio.wait(
                                    pending, return_when=asyncio.FIRST_COMPLETED
                                )
                                for task in done2:
                                    result = task.result()
                                    if result:
                                        engine_name, rows = result
                                        if engine_name in (
                                            "duckduckgo",
                                            "duckduckgo-jina",
                                        ):
                                            return engine_name, rows
                    except asyncio.TimeoutError:
                        pass

                    return results["bing"]

    except asyncio.TimeoutError:
        pass
    except Exception:
        logger.warning(
            "Concurrent free search failed unexpectedly", exc_info=True
        )
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    for engine in ("duckduckgo", "duckduckgo-jina", "bing"):
        if engine in results:
            return results[engine]

    raise RuntimeError("All engines returned empty results")


def _engine_display_name(engine: str) -> str:
    mapping = {
        "duckduckgo": "DuckDuckGo",
        "duckduckgo-jina": "DuckDuckGo (via jina.ai)",
        "bing": "Bing",
    }
    return mapping.get(engine, engine)


def _resolve_petal_search_url() -> str:
    """Build Petal Search URL from LLM API_BASE: strip trailing /v2, append /v1/ai-tools/web-search."""
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
    """Same JSON object as LLM client (e.g. Authorization); env name ``default_headers``."""
    headers = build_default_headers()
    if not headers:
        raise ValueError("default_headers is not set (use the same JSON headers as the LLM, e.g. Authorization)")
    if not isinstance(headers, dict):
        raise ValueError("default_headers must be a JSON object")
    return {str(k): str(v) for k, v in headers.items() if v is not None}


def enable_petal_search() -> bool:
    """Return whether Petal web-search is available for current runtime."""
    try:
        from jiuwenclaw.config import get_config_raw

        if not (get_config_raw().get("enable_petal_web_search") or False):
            return False
    except Exception:
        logger.warning(
            "Failed to read enable_petal_web_search config", exc_info=True
        )
        return False
    api_base = (
        os.environ.get("API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or ""
    ).strip()
    if not api_base:
        return False
    parsed = build_default_headers()
    return parsed is not None and isinstance(parsed, dict)


# Align with common search-record assembly (title / url / content); avoid huge tool payloads.
_PETAL_MAX_TITLE_LEN = 2000
_PETAL_MAX_URL_LEN = 2048
_PETAL_MAX_CONTENT_LEN = 8000


def _petal_normalize_web_page_item(item: dict[str, Any]) -> dict[str, str]:
    """Map Petal ``web_pages[]`` entry to page record fields (same shape as collector ``new_item``)."""
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
    """One block per result: title, URL, content (same layout idea as ``mcp_free_search``)."""
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


def _petal_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    """Petal Search API: POST JSON with query + content; response.web_pages[]."""
    search_url = _resolve_petal_search_url()
    header_map = _load_llm_default_headers()
    headers = {**header_map, "Content-Type": "application/json"}
    payload = {"query": query, "content": True}

    response = _http_request(
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


def _perplexity_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not perplexity_key:
        raise ValueError("PERPLEXITY_API_KEY is not set")

    payload = {
        "model": os.environ.get("PPLX_MODEL", "sonar-pro"),
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
    response = _http_request(
        "POST",
        os.environ.get("PPLX_API_URL", "https://api.perplexity.ai/chat/completions"),
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


def _serper_search_sync(
    query: str, max_results: int, timeout_seconds: int
) -> dict[str, Any]:
    serper_key = os.environ.get("SERPER_API_KEY", "")
    if not serper_key:
        raise ValueError("SERPER_API_KEY is not set")

    response = _http_request(
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


def _jina_search_sync(query: str, timeout_seconds: int) -> dict[str, Any]:
    jina_key = os.environ.get("JINA_API_KEY", "")
    if not jina_key:
        raise ValueError("JINA_API_KEY is not set")

    payload = {
        "model": "jina-deepsearch-v1",
        "messages": [{"role": "user", "content": query}],
        "stream": False,
        "reasoning_effort": "low",
    }
    response = _http_request(
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


@tool(
    name="mcp_free_search",
    description=(
        "Fallback or second-pass web search via DuckDuckGo/Bing. "
        "Do not call in the same step as mcp_petal_search; first observe the Petal result, "
        "then use this only if Petal failed, returned empty results, or lacks needed coverage. "
        "Input query and return ranked URLs with snippets."
    ),
)
async def mcp_free_search(
    query: str,
    max_results: int = 8,
    timeout_seconds: int = 5,
    overall_timeout: int = 5,
) -> str:
    query = (query or "").strip()
    if not query:
        return "[ERROR]: query cannot be empty."

    max_results = max(1, min(max_results, 20))
    timeout_seconds = max(3, min(timeout_seconds, 10))
    overall_timeout = 5

    try:
        engine_used, rows = await _search_free_async(
            query, max_results, timeout_seconds, overall_timeout
        )
    except Exception as exc:
        try:
            engine_used, rows = await asyncio.to_thread(
                _search_free_sync, query, max_results, timeout_seconds
            )
        except Exception as fallback_exc:
            return f"[ERROR]: free search failed: {exc}, fallback also failed: {fallback_exc}"

    if not rows:
        return f"No search results for: {query}"

    lines = [f"Free search results ({_engine_display_name(engine_used)}) for: {query}"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['title']}")
        lines.append(f"   URL: {row['url']}")
        if row.get("snippet"):
            lines.append(f"   Snippet: {row['snippet']}")
    return "\n".join(lines)


@tool(
    name="mcp_paid_search",
    description=(
        "Paid search via Petal/Perplexity/SERPER/JINA. "
        "provider=auto|petal|perplexity|serper|jina."
    ),
)
async def mcp_paid_search(
    query: str,
    provider: str = "auto",
    max_results: int = 8,
    timeout_seconds: int = 45,
) -> str:
    query = (query or "").strip()
    if not query:
        return "[ERROR]: query cannot be empty."

    provider = (provider or "auto").strip().lower()
    if provider not in {"auto", "jina", "serper", "perplexity", "petal"}:
        return "[ERROR]: provider must be one of auto|petal|jina|serper|perplexity."

    timeout_seconds = max(10, min(timeout_seconds, 120))
    max_results = max(1, min(max_results, 20))

    runners = {
        "petal": lambda: _petal_search_sync(
            query=query, max_results=max_results, timeout_seconds=timeout_seconds
        ),
        "jina": lambda: _jina_search_sync(query=query, timeout_seconds=timeout_seconds),
        "serper": lambda: _serper_search_sync(
            query=query, max_results=max_results, timeout_seconds=timeout_seconds
        ),
        "perplexity": lambda: _perplexity_search_sync(
            query=query, max_results=max_results, timeout_seconds=timeout_seconds
        ),
    }
    order = (
        [provider]
        if provider != "auto"
        else ["perplexity", "serper", "jina", "petal"]
    )

    errors: list[str] = []
    for name in order:
        try:
            result = await asyncio.to_thread(runners[name])
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue

        answer = str(result.get("answer", "") or "").strip()
        urls = [str(u) for u in (result.get("urls", []) or []) if u][:max_results]
        lines = [f"Paid search provider: {name}"]
        if answer:
            lines.append("Answer:")
            lines.append(answer)
        if urls:
            lines.append("URLs:")
            for idx, url in enumerate(urls, 1):
                lines.append(f"{idx}. {url}")
        if not answer and not urls:
            lines.append("No usable result payload.")
        return "\n".join(lines)

    return "[ERROR]: paid search failed. " + " | ".join(errors)
