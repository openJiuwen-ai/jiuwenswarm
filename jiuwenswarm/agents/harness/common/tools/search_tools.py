# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Search tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
import urllib3
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import env_url

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}
_FREE_SEARCH_DDG_ENABLED_ENV = "FREE_SEARCH_DDG_ENABLED"
_FREE_SEARCH_BING_ENABLED_ENV = "FREE_SEARCH_BING_ENABLED"
_FREE_SEARCH_PROXY_URL_ENV = "FREE_SEARCH_PROXY_URL"
_FREE_SEARCH_SSL_VERIFY_ENV = "FREE_SEARCH_SSL_VERIFY"
_FREE_SEARCH_DDG_URL_ENV = "FREE_SEARCH_DDG_URL"
_FREE_SEARCH_DEFAULT_NO_PROXY = (
    "127.0.0.1,.huawei.com,localhost,local,.local,10.155.97.247,.myhuaweicloud.com"
)


def _get_free_search_proxy_url() -> str:
    return str(os.environ.get(_FREE_SEARCH_PROXY_URL_ENV, "") or "").strip()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _env_flag(name: str, default: bool = False) -> bool:
    return _env_bool(name, default=default)


def _free_search_ssl_verify() -> bool:
    return _env_bool(_FREE_SEARCH_SSL_VERIFY_ENV, default=False)


def _disable_insecure_request_warning() -> None:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _duckduckgo_search_url(query: str) -> str:
    base_url = (
        os.environ.get(_FREE_SEARCH_DDG_URL_ENV, "https://html.duckduckgo.com/html/")
        or "https://html.duckduckgo.com/html/"
    ).strip()
    separator = "&" if "?" in base_url else "?"
    return f"{base_url.rstrip('?&')}{separator}q={quote_plus(query)}"


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


def _apply_free_search_proxy(url: str, kwargs: dict[str, Any]) -> bool:
    proxy_url = _get_free_search_proxy_url()
    if not proxy_url or _should_bypass_free_search_proxy(url):
        return False
    kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})
    return True


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    """Try normal request first; retry without env proxies on ProxyError."""
    kwargs.setdefault("verify", get_requests_verify())
    method_up = method.upper()
    explicit_proxy = _apply_free_search_proxy(url, kwargs)
    if "verify" not in kwargs:
        kwargs["verify"] = _free_search_ssl_verify()
        if kwargs["verify"] is False:
            _disable_insecure_request_warning()
    try:
        if method_up == "GET":
            return requests.get(url, **kwargs)
        if method_up == "POST":
            return requests.post(url, **kwargs)
        return requests.request(method_up, url, **kwargs)
    except requests.exceptions.ProxyError:
        if explicit_proxy:
            raise
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
            decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8")).decode(
                "utf-8", errors="ignore"
            )
            if decoded.startswith(("http://", "https://")):
                return decoded
        except Exception:
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


def _search_duckduckgo_sync(query: str, max_results: int, timeout_seconds: int) -> list[dict[str, str]]:
    url = _duckduckgo_search_url(query)
    response = _http_request("GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
    if _is_ddg_challenge_page(response.status_code, response.text):
        raise RuntimeError("DuckDuckGo anti-bot challenge page returned")
    if response.status_code != 200:
        raise RuntimeError(f"DuckDuckGo returned non-200 status: {response.status_code}")
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
    response = _http_request("GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    text = response.text or ""

    # Parse markdown links rendered by r.jina.ai.
    matches = re.findall(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", text, flags=re.IGNORECASE)

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


def _search_bing_sync(query: str, max_results: int, timeout_seconds: int) -> list[dict[str, str]]:
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    response = _http_request("GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds)
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
        snippet_match = re.search(r"<p>(.*?)</p>", block, flags=re.IGNORECASE | re.DOTALL)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        rows.append({"title": title or f"Result {len(rows) + 1}", "url": href, "snippet": snippet})
        if len(rows) >= max_results:
            break

    return rows


def _search_free_sync(
    query: str, max_results: int, timeout_seconds: int
) -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    engines = []
    if _env_flag(_FREE_SEARCH_DDG_ENABLED_ENV, default=False):
        engines.extend([
            ("duckduckgo", _search_duckduckgo_sync),
            ("duckduckgo-jina", _search_duckduckgo_via_jina_sync),
        ])
    if _env_flag(_FREE_SEARCH_BING_ENABLED_ENV, default=False):
        engines.append(("bing", _search_bing_sync))
    if not engines:
        raise RuntimeError("all free search engines are disabled")
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


def _engine_display_name(engine: str) -> str:
    mapping = {
        "duckduckgo": "DuckDuckGo",
        "duckduckgo-jina": "DuckDuckGo (via jina.ai)",
        "bing": "Bing",
    }
    return mapping.get(engine, engine)


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
                maybe_url = item.get("url") or item.get("link") or item.get("source_url")
                if maybe_url:
                    urls.append(str(maybe_url))
        if urls:
            return urls
    return []


def _perplexity_search_sync(query: str, max_results: int, timeout_seconds: int) -> dict[str, Any]:
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not perplexity_key:
        raise ValueError("PERPLEXITY_API_KEY is not set")

    payload = {
        "model": os.environ.get("PPLX_MODEL", "sonar-pro"),
        "messages": [
            {"role": "system", "content": "Provide concise answer and include citations."},
            {"role": "user", "content": query},
        ],
        "max_tokens": 1024,
        "temperature": 0.2,
        "stream": False,
    }
    response = _http_request(
        "POST",
        env_url("PPLX_API_URL", "https://api.perplexity.ai/chat/completions"),
        headers={"Authorization": f"Bearer {perplexity_key}", "Content-Type": "application/json"},
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


def _extract_bocha_urls(data: dict[str, Any], max_results: int) -> list[str]:
    candidates: list[Any] = []
    for container in (
        data.get("data", {}).get("webPages", {}).get("value"),
        data.get("webPages", {}).get("value"),
        data.get("data", {}).get("webPages"),
        data.get("webPages"),
        data.get("data", {}).get("results"),
        data.get("results"),
    ):
        if isinstance(container, list):
            candidates = container
            break

    urls: list[str] = []
    for item in candidates[:max_results]:
        if not isinstance(item, dict):
            continue
        maybe_url = item.get("url") or item.get("link")
        if maybe_url:
            urls.append(str(maybe_url))
    return urls


def _extract_bocha_answer(data: dict[str, Any]) -> str:
    candidates = [
        data.get("summary"),
        data.get("answer"),
        data.get("data", {}).get("summary"),
        data.get("data", {}).get("answer"),
        data.get("data", {}).get("message"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    web_pages = data.get("data", {}).get("webPages", {})
    if isinstance(web_pages, dict):
        value = web_pages.get("value")
        if isinstance(value, list):
            snippets: list[str] = []
            for item in value[:3]:
                if not isinstance(item, dict):
                    continue
                snippet = item.get("summary") or item.get("snippet")
                if isinstance(snippet, str) and snippet.strip():
                    snippets.append(snippet.strip())
            if snippets:
                return "\n\n".join(snippets[:3])
    return ""


def _bocha_search_sync(query: str, max_results: int, timeout_seconds: int) -> dict[str, Any]:
    bocha_key = os.environ.get("BOCHA_API_KEY", "")
    if not bocha_key:
        raise ValueError("BOCHA_API_KEY is not set")

    response = _http_request(
        "POST",
        env_url("BOCHA_API_URL", "https://api.bocha.cn/v1/web-search"),
        headers={"Authorization": f"Bearer {bocha_key}", "Content-Type": "application/json"},
        json={"query": query, "summary": True, "count": max_results},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "provider": "bocha",
        "answer": _extract_bocha_answer(data),
        "urls": _extract_bocha_urls(data, max_results),
    }


def _serper_search_sync(query: str, max_results: int, timeout_seconds: int) -> dict[str, Any]:
    serper_key = os.environ.get("SERPER_API_KEY", "")
    if not serper_key:
        raise ValueError("SERPER_API_KEY is not set")

    response = _http_request(
        "POST",
        env_url("SERPER_API_URL", "https://google.serper.dev/search"),
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
        env_url("JINA_API_URL", "https://deepsearch.jina.ai/v1/chat/completions"),
        headers={"Authorization": f"Bearer {jina_key}", "Content-Type": "application/json"},
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
    description="Free search via DuckDuckGo. Input query and return ranked URLs with snippets.",
)
async def mcp_free_search(query: str, max_results: int = 8, timeout_seconds: int = 20) -> str:
    query = (query or "").strip()
    if not query:
        return "[ERROR]: query cannot be empty."

    max_results = max(1, min(max_results, 20))
    timeout_seconds = max(5, min(timeout_seconds, 60))
    try:
        engine_used, rows = await asyncio.to_thread(
            _search_free_sync, query, max_results, timeout_seconds
        )
    except Exception as exc:
        return f"[ERROR]: free search failed: {exc}"

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
    description="Paid search via Bocha/Perplexity/SERPER/JINA. Support provider=auto|bocha|perplexity|serper|jina.",
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
    if provider not in {"auto", "bocha", "jina", "serper", "perplexity"}:
        return "[ERROR]: provider must be one of auto|bocha|jina|serper|perplexity."

    timeout_seconds = max(10, min(timeout_seconds, 120))
    max_results = max(1, min(max_results, 20))

    runners = {
        "bocha": lambda: _bocha_search_sync(
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
    
    available_providers = []
    if os.environ.get("BOCHA_API_KEY"):
        available_providers.append("bocha")
    if os.environ.get("PERPLEXITY_API_KEY"):
        available_providers.append("perplexity")
    if os.environ.get("SERPER_API_KEY"):
        available_providers.append("serper")
    if os.environ.get("JINA_API_KEY"):
        available_providers.append("jina")
    
    if not available_providers:
        return "[ERROR]: no paid search API keys configured."
    
    if provider != "auto":
        if provider not in available_providers:
            return f"[ERROR]: {provider} API key not configured. Available providers: {', '.join(available_providers)}"
        order = [provider]
    else:
        order = [p for p in ["bocha", "perplexity", "serper", "jina"] if p in available_providers]

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
