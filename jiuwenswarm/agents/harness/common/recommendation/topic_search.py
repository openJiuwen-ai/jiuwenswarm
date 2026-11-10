# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Search adapters for topic recommendation (OpenJiuwen native free search)."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
)

OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID = "openjiuwen-native-free-search"
# Serving identity while no web-search lane is usable. Host-evidence and hot-list
# pools still generate under it; enabling a real lane changes the compatibility key.
UNCONFIGURED_SEARCH_PROVIDER_ID = "search-unconfigured"
OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION = (
    "topic-starter-v6-openjiuwen-native-free-search-v1"
)
OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION = (
    "topic-starter-v6-openjiuwen-native-free-search-failure-v1"
)
OPENJIUWEN_NATIVE_SEARCH_ENGINES = frozenset(
    {"duckduckgo", "duckduckgo-jina", "bing"}
)
LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID = "openjiuwen-native-search"
LEGACY_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION = (
    "topic-starter-v6-openjiuwen-native-search-v1"
)
LEGACY_OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION = (
    "topic-starter-v6-openjiuwen-native-search-failure-v1"
)
OPENJIUWEN_NATIVE_SEARCH_MAX_CONCURRENCY = 3
_OPENJIUWEN_WEB_SEARCH_ENGINE_ERROR_CODE = 182600
_OPENJIUWEN_WEB_SEARCH_ALL_ENGINES_FAILED_CODE = 182601
_NON_200_STATUS_PATTERN = re.compile(r"non-200 status: ([1-5][0-9]{2})\Z")
OPENJIUWEN_NATIVE_SEARCH_FAILURE_REASON_BY_KIND = {
    "all_engines_failed": "provider_all_engines_failed",
    "challenge": "provider_challenge",
    "http_status": "provider_http_status",
    "low_relevance": "provider_low_relevance",
    "timeout": "provider_timeout",
    "proxy": "provider_proxy",
    "transport": "provider_transport",
    "unknown": "provider_unknown",
}

SearchRunner = Callable[
    [str, int, int],
    Awaitable[tuple[str, list[dict[str, str]]]],
]
NativeFactory = Callable[[Mapping[str, str]], Any]


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _request_id() -> str:
    return f"openjiuwen-native-search-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class OpenJiuwenNativeSearchResult:
    engine: str
    rows: tuple[dict[str, str], ...]
    receipt: dict[str, Any]


class OpenJiuwenNativeSearchContractError(ValueError):
    """One stable fail-closed reason with its host-owned request ID."""

    def __init__(self, reason: str, *, host_request_id: str = "") -> None:
        self.reason = reason
        self.host_request_id = host_request_id
        super().__init__(reason)


class OpenJiuwenNativeSearchProviderError(RuntimeError):
    """One redacted provider failure with its host-owned request ID."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self.receipt = dict(receipt)
        self.host_request_id = str(receipt["host_request_id"])
        self.reason = OPENJIUWEN_NATIVE_SEARCH_FAILURE_REASON_BY_KIND[
            str(receipt["failure_kind"])
        ]
        super().__init__(self.reason)


def _provider_failure_kind(exc: Exception) -> tuple[str, int | None]:
    params = getattr(exc, "params", None)
    if getattr(exc, "code", None) == _OPENJIUWEN_WEB_SEARCH_ALL_ENGINES_FAILED_CODE:
        return "all_engines_failed", None
    if (
        getattr(exc, "code", None) == _OPENJIUWEN_WEB_SEARCH_ENGINE_ERROR_CODE
        and isinstance(params, Mapping)
        and params.get("engine") == "duckduckgo"
    ):
        provider_reason = params.get("reason")
        if provider_reason == "anti-bot challenge page returned":
            return "challenge", None
        if isinstance(provider_reason, str):
            status_match = _NON_200_STATUS_PATTERN.fullmatch(provider_reason)
            if status_match is not None:
                return "http_status", int(status_match.group(1))
        return "transport", None
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", None
    if isinstance(
        exc,
        (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError),
    ):
        return "proxy", None
    if isinstance(exc, aiohttp.ClientResponseError):
        status = exc.status
        if _plain_int(status) and 100 <= status <= 599:
            return "http_status", status
    if isinstance(exc, (aiohttp.ClientError, ConnectionError, OSError)):
        return "transport", None
    return "unknown", None


def _provider_failure_receipt(
    exc: Exception,
    *,
    host_request_id: str,
) -> dict[str, Any]:
    failure_kind, http_status = _provider_failure_kind(exc)
    return {
        "receipt_version": OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION,
        "provider_id": OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
        "host_request_id": host_request_id,
        "engine": "openjiuwen-free-search",
        "failure_kind": failure_kind,
        "http_status": http_status,
    }


async def _default_search_runner(
    query: str,
    max_results: int,
    timeout_seconds: int,
) -> tuple[str, list[dict[str, str]]]:
    from openjiuwen.harness.tools.web import _http
    from openjiuwen.harness.tools.web.free_search import (
        WebFreeSearchTool,
        _FreeSearchRequest,
    )

    async with _http.new_session() as session:
        # The free-search engine ships in openjiuwen; its private entry point is the
        # only way in, so the call crosses that boundary by necessity.
        engine, rows = await WebFreeSearchTool._search_free(  # pylint: disable=protected-access
            _FreeSearchRequest(
                session=session,
                query=query,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
            ),
        )
    return engine, rows


class OpenJiuwenNativeSearch:
    """Structured facade over OpenJiuwen's native free-search tool."""

    provider_id = OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID

    def __init__(
        self,
        *,
        search_runner: SearchRunner = _default_search_runner,
        request_id_factory: Callable[[], str] = _request_id,
    ) -> None:
        self._search_runner = search_runner
        self._request_id_factory = request_id_factory
        self._search_semaphore = asyncio.Semaphore(
            OPENJIUWEN_NATIVE_SEARCH_MAX_CONCURRENCY
        )
        self.invocation_receipts: list[dict[str, Any]] = []

    async def __call__(
        self,
        query: str,
        max_results: int,
        timeout_seconds: int,
    ) -> OpenJiuwenNativeSearchResult:
        host_request_id = str(self._request_id_factory() or "").strip()
        _rejected3 = (
            not host_request_id
            or not isinstance(query, str)
            or not query.strip()
            or not 1 <= max_results <= 20
            or timeout_seconds <= 0
        )
        if (
            _rejected3
        ):
            raise OpenJiuwenNativeSearchContractError(
                "search_request_contract_failed",
                host_request_id=host_request_id,
            )
        try:
            async with self._search_semaphore:
                engine, raw_rows = await self._search_runner(
                    query,
                    max_results,
                    timeout_seconds,
                )
        except Exception as exc:
            raise OpenJiuwenNativeSearchProviderError(
                _provider_failure_receipt(
                    exc,
                    host_request_id=host_request_id,
                )
            ) from None
        if engine not in OPENJIUWEN_NATIVE_SEARCH_ENGINES or not isinstance(
            raw_rows, list
        ):
            raise OpenJiuwenNativeSearchContractError(
                "search_result_contract_failed",
                host_request_id=host_request_id,
            )
        rows: list[dict[str, str]] = []
        urls: list[str] = []
        for raw_row in raw_rows[:max_results]:
            if not isinstance(raw_row, dict):
                raise OpenJiuwenNativeSearchContractError(
                    "search_result_contract_failed",
                    host_request_id=host_request_id,
                )
            url = str(raw_row.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise OpenJiuwenNativeSearchContractError(
                    "search_result_contract_failed",
                    host_request_id=host_request_id,
                )
            rows.append(dict(raw_row))
            urls.append(url)
        if not rows or len(urls) != len(set(urls)):
            raise OpenJiuwenNativeSearchContractError(
                "search_result_contract_failed",
                host_request_id=host_request_id,
            )
        receipt = {
            "receipt_version": OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
            "provider_id": OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
            "host_request_id": host_request_id,
            "requested_query": query,
            "engine": engine,
            "result_urls": urls,
            "result_count": len(rows),
        }
        result = OpenJiuwenNativeSearchResult(
            engine=engine,
            rows=tuple(rows),
            receipt=receipt,
        )
        self.invocation_receipts.append(receipt)
        return result


def build_openjiuwen_native_search(
    *,
    environ: Mapping[str, str] | None = None,
    search_runner: SearchRunner = _default_search_runner,
    request_id_factory: Callable[[], str] = _request_id,
) -> OpenJiuwenNativeSearch:
    # Free-search engine toggles no longer gate construction: topic recommendation
    # (hot-list pull, presets, model work) must start without them. A call still
    # fails at search time when both engines are off.
    del environ
    return OpenJiuwenNativeSearch(
        search_runner=search_runner,
        request_id_factory=request_id_factory,
    )


def _native_factory(values: Mapping[str, str]) -> OpenJiuwenNativeSearch:
    return build_openjiuwen_native_search(environ=values)


def _env_flag_enabled(values: Mapping[str, str], name: str) -> bool:
    return str(values.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def topic_web_search_engines_enabled(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether at least one free-search engine toggle can enrich web candidates."""

    values = os.environ if environ is None else environ
    return _env_flag_enabled(values, "FREE_SEARCH_DDG_ENABLED") or _env_flag_enabled(
        values, "FREE_SEARCH_BING_ENABLED"
    )


def selected_topic_search_provider_id(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the exact serving-compatibility identity of the selected lane.

    The native free-search lane is the only one implemented, so an unset variable
    selects it. Free-search engine toggles do not choose the lane: topic
    recommendation stays available when they are off. An unknown explicit name
    still fails loudly.
    """

    values = os.environ if environ is None else environ
    provider = str(
        values.get("TOPIC_STARTER_SEARCH_PROVIDER")
        or values.get("TOPIC_STARTER_V6_SEARCH_PROVIDER")
        or ""
    ).strip()
    if provider in ("", "openjiuwen-native"):
        return OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
    raise OpenJiuwenNativeSearchContractError("search_provider_not_configured")


def build_topic_search_provider(
    config: ArkBackgroundConfig,
    *,
    environ: Mapping[str, str] | None = None,
    native_factory: NativeFactory = _native_factory,
) -> Any:
    """Construct exactly the search lane selected at process startup."""

    del config
    values = os.environ if environ is None else environ
    selected_topic_search_provider_id(environ=values)
    return native_factory(values)


# Legacy receipt identities kept for reading older pools. Generate uses the
# OpenJiuwen native free-search lane above; the former Ark Responses web-search
# client was removed as dead production code.
ARK_WEB_SEARCH_PROVIDER_ID = "ark-responses-web-search"
ARK_WEB_SEARCH_RECEIPT_VERSION = "topic-starter-v6-ark-responses-web-search-v1"
ARK_WEB_SEARCH_ENTITLEMENT_RECEIPT_VERSION = (
    "topic-starter-v6-ark-responses-web-search-entitlement-v1"
)
