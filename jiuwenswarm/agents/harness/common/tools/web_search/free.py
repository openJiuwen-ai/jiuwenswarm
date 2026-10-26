# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Free search engines (DuckDuckGo, Bing) for web_search fallback."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from html import unescape
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from jiuwenswarm.agents.harness.common.tools.web_search.http_client import http_request
from jiuwenswarm.common.local_env_config import read_env_if_set

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = read_env_if_set(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"false", "0", "no", "off"}


def _free_search_engines() -> list[tuple[str, object]]:
    engines: list[tuple[str, object]] = []
    if _env_flag("FREE_SEARCH_DDG_ENABLED"):
        engines.append(("duckduckgo", _search_duckduckgo_sync))
        engines.append(("duckduckgo-jina", _search_duckduckgo_via_jina_sync))
    if _env_flag("FREE_SEARCH_BING_ENABLED"):
        engines.append(("bing", _search_bing_sync))
    return engines


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
    response = http_request(
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
    response = http_request(
        "GET", url, headers=_REQUEST_HEADERS, timeout=timeout_seconds
    )
    response.raise_for_status()
    text = response.text or ""

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
    response = http_request(
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


def search_free_sync(
    query: str, max_results: int, timeout_seconds: int
) -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    engines = _free_search_engines()
    if not engines:
        raise RuntimeError("All free search engines disabled by configuration")
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


async def search_free_async(
    query: str, max_results: int, timeout_seconds: int, overall_timeout: int
) -> tuple[str, list[dict[str, str]]]:
    """Concurrent search with quality-first fallback strategy."""
    engines = _free_search_engines()
    if not engines:
        raise RuntimeError("All free search engines disabled by configuration")

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
