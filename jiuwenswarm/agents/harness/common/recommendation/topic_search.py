# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Search adapters for topic recommendation: OpenJiuwen native search and Ark web search."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
from openai import APIStatusError

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
)

OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID = "openjiuwen-native-free-search"
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
    values = os.environ if environ is None else environ
    _require_native_free_search(values)
    return OpenJiuwenNativeSearch(
        search_runner=search_runner,
        request_id_factory=request_id_factory,
    )


def _require_native_free_search(values: Mapping[str, str]) -> None:
    if (
        str(values.get("FREE_SEARCH_DDG_ENABLED") or "").strip().lower()
        != "true"
        or str(values.get("FREE_SEARCH_BING_ENABLED") or "").strip().lower()
        != "true"
    ):
        raise OpenJiuwenNativeSearchContractError(
            "openjiuwen_native_search_requires_free_engines"
        )


def _native_factory(values: Mapping[str, str]) -> OpenJiuwenNativeSearch:
    return build_openjiuwen_native_search(environ=values)


def selected_topic_search_provider_id(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the exact serving-compatibility identity of the selected lane.

    The native free-search lane is the only one implemented, so an unset variable selects it:
    the model comes from the settings page, and the search toggles from theirs. The variable
    remains only to name the lane explicitly; an unknown explicit name still fails loudly.
    """

    values = os.environ if environ is None else environ
    provider = str(
        values.get("TOPIC_STARTER_SEARCH_PROVIDER")
        or values.get("TOPIC_STARTER_V6_SEARCH_PROVIDER")
        or ""
    ).strip()
    if provider in ("", "openjiuwen-native"):
        _require_native_free_search(values)
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

ARK_WEB_SEARCH_PROVIDER_ID = "ark-responses-web-search"
ARK_WEB_SEARCH_RECEIPT_VERSION = "topic-starter-v6-ark-responses-web-search-v1"
ARK_WEB_SEARCH_ENTITLEMENT_RECEIPT_VERSION = (
    "topic-starter-v6-ark-responses-web-search-entitlement-v1"
)
_ENTITLEMENT_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "provider_id",
        "actual_model",
        "operation",
        "tool_type",
        "provider_error_code",
        "http_status",
        "provider_request_id",
        "observed_at",
    }
)
_SEARCH_INSTRUCTIONS = (
    "Call web_search exactly once. Use the user input verbatim as the search "
    "action query. Return a concise answer with URL citations."
)


@dataclass(frozen=True, slots=True)
class ArkWebSearchResult:
    engine: str
    rows: tuple[dict[str, str], ...]
    receipt: dict[str, Any]


class ArkWebSearchContractError(ValueError):
    """One stable fail-closed reason from the host-owned response parser."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ArkWebSearchEntitlementError(RuntimeError):
    """Redacted evidence that the real provider rejected the Web Search tool."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self.receipt = dict(receipt)
        super().__init__("ark_web_search_entitlement_blocked")


def _provider_error_code(exc: APIStatusError) -> str | None:
    body = exc.body
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    error = nested if isinstance(nested, Mapping) else body
    code = error.get("code")
    return code if isinstance(code, str) else None


def _tool_not_open_receipt(
    exc: APIStatusError,
    *,
    actual_model: str,
) -> dict[str, Any] | None:
    request_id = exc.request_id
    if _provider_error_code(exc) != "ToolNotOpen":
        return None
    if not 400 <= exc.status_code < 500:
        return None
    if not isinstance(request_id, str):
        return None
    if not request_id.strip():
        return None
    return {
        "receipt_version": ARK_WEB_SEARCH_ENTITLEMENT_RECEIPT_VERSION,
        "provider_id": ARK_WEB_SEARCH_PROVIDER_ID,
        "actual_model": actual_model,
        "operation": "responses.create",
        "tool_type": "web_search",
        "provider_error_code": "ToolNotOpen",
        "http_status": exc.status_code,
        "provider_request_id": request_id.strip(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _default_client_factory(**kwargs: Any) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(**kwargs)


def _response_mapping(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    model_dump = getattr(response, "model_dump", None)
    if not callable(model_dump):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    payload = model_dump(exclude_none=True)
    if not isinstance(payload, Mapping):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    return payload


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _required_text(value: Any, *, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArkWebSearchContractError(reason)
    return value.strip()


def _parse_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    tool_usage = usage.get("tool_usage")
    if not isinstance(tool_usage, Mapping):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    web_search_count = _nonnegative_integer(tool_usage.get("web_search"))
    if web_search_count is None or web_search_count < 1:
        raise ArkWebSearchContractError("response_metadata_mismatch")
    receipt: dict[str, Any] = {
        "tool_usage": {"web_search": web_search_count},
    }
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        count = _nonnegative_integer(usage.get(field))
        if count is not None:
            receipt[field] = count
    return {
        field: receipt[field]
        for field in ("input_tokens", "output_tokens", "total_tokens", "tool_usage")
        if field in receipt
    }


def _parse_url_citations(
    output: list[Any],
    *,
    max_results: int,
) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if item.get("status") != "completed":
            raise ArkWebSearchContractError("response_metadata_mismatch")
        content = item.get("content")
        if not isinstance(content, list):
            raise ArkWebSearchContractError("missing_url_citation")
        for content_item in content:
            if not isinstance(content_item, Mapping):
                raise ArkWebSearchContractError("missing_url_citation")
            annotations = content_item.get("annotations")
            if annotations is None:
                continue
            if not isinstance(annotations, list):
                raise ArkWebSearchContractError("url_citation_contract_failed")
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    raise ArkWebSearchContractError(
                        "url_citation_contract_failed"
                    )
                if annotation.get("type") != "url_citation":
                    continue
                url = _required_text(
                    annotation.get("url"),
                    reason="url_citation_contract_failed",
                )
                parsed_url = urlparse(url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                    raise ArkWebSearchContractError(
                        "url_citation_contract_failed"
                    )
                row = {"url": url}
                for field in (
                    "title",
                    "summary",
                    "site_name",
                    "publish_time",
                    "freshness_info",
                ):
                    if field not in annotation:
                        continue
                    row[field] = _required_text(
                        annotation[field],
                        reason="url_citation_contract_failed",
                    )
                if url not in seen_urls:
                    seen_urls.add(url)
                    rows.append(row)
    if not rows:
        raise ArkWebSearchContractError("missing_url_citation")
    return tuple(rows[:max_results])


def _parse_response(
    response: Any,
    *,
    expected_model: str,
    query: str,
    max_results: int,
) -> ArkWebSearchResult:
    payload = _response_mapping(response)
    response_id = payload.get("id")
    _rejected = (
        not isinstance(response_id, str)
        or not response_id
        or payload.get("object") != "response"
        or payload.get("status") != "completed"
        or payload.get("model") != expected_model
    )
    if (
        _rejected
    ):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    output = payload.get("output")
    if not isinstance(output, list):
        raise ArkWebSearchContractError("response_metadata_mismatch")
    search_calls = [
        item
        for item in output
        if isinstance(item, Mapping) and item.get("type") == "web_search_call"
    ]
    if not search_calls:
        raise ArkWebSearchContractError("missing_web_search_call")
    if len(search_calls) != 1:
        raise ArkWebSearchContractError("multiple_web_search_calls")
    search_call = search_calls[0]
    call_id = search_call.get("id")
    action = search_call.get("action")
    _rejected2 = (
        not isinstance(call_id, str)
        or not call_id
        or search_call.get("status") != "completed"
        or not isinstance(action, Mapping)
        or action.get("type") != "search"
    )
    if (
        _rejected2
    ):
        raise ArkWebSearchContractError("web_search_call_metadata_mismatch")
    action_query = action.get("query")
    if action_query != query:
        raise ArkWebSearchContractError("search_query_mismatch")
    usage = _parse_usage(payload)
    rows = _parse_url_citations(output, max_results=max_results)
    receipt = {
        "receipt_version": ARK_WEB_SEARCH_RECEIPT_VERSION,
        "provider_id": ARK_WEB_SEARCH_PROVIDER_ID,
        "provider_response_id": response_id,
        "response_object": "response",
        "response_status": "completed",
        "actual_model": expected_model,
        "web_search_call_id": call_id,
        "action": {"type": "search", "query": query},
        "usage": usage,
        "result_count": len(rows),
    }
    return ArkWebSearchResult(
        engine=ARK_WEB_SEARCH_PROVIDER_ID,
        rows=rows,
        receipt=receipt,
    )


class ArkResponsesWebSearch:
    """One config-bound async Responses Web Search provider."""

    provider_id = ARK_WEB_SEARCH_PROVIDER_ID

    def __init__(
        self,
        config: ArkBackgroundConfig,
        *,
        client_factory: Callable[..., Any] = _default_client_factory,
    ) -> None:
        self._config = config
        self._client = client_factory(api_key=config.api_key, base_url=config.base_url)
        self._concurrency = asyncio.Semaphore(5)
        self.invocation_receipts: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        await self._client.close()

    async def __call__(
        self,
        query: str,
        max_results: int,
        timeout_seconds: int,
    ) -> ArkWebSearchResult:
        if not isinstance(query, str) or not query.strip():
            raise ArkWebSearchContractError("search_request_contract_failed")
        if not 1 <= max_results <= 50 or timeout_seconds <= 0:
            raise ArkWebSearchContractError("search_request_contract_failed")
        request = {
            "model": self._config.model,
            "input": query,
            "instructions": _SEARCH_INSTRUCTIONS,
            "tools": [
                {"type": "web_search", "max_keyword": 1, "limit": max_results}
            ],
            "tool_choice": "required",
            "max_tool_calls": 1,
            "store": False,
            "temperature": self._config.effective_temperature,
            "top_p": self._config.effective_top_p,
        }
        async with self._concurrency:
            try:
                response = await asyncio.wait_for(
                    self._client.responses.create(**request),
                    timeout=timeout_seconds,
                )
            except APIStatusError as exc:
                receipt = _tool_not_open_receipt(
                    exc,
                    actual_model=self._config.model,
                )
                if receipt is None:
                    raise
                raise ArkWebSearchEntitlementError(receipt) from None
        result = _parse_response(
            response,
            expected_model=self._config.expected_model or self._config.model,
            query=query,
            max_results=max_results,
        )
        self.invocation_receipts.append(result.receipt)
        return result


