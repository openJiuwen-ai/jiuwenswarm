# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.http.fetch``: one hardened outbound request for a Gateway package.

The policy is closed by default: an empty host allowlist denies everything,
redirects are followed only when the caller opts in *and* every hop is allowed,
and a response larger than the configured body budget is replaced by an error
code instead of being truncated.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urljoin

import httpx
from openjiuwen.core.events import (
    Event,
    EventHandler,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
)

from jiuwenswarm.gateway.extensions_v2.config import MAX_HTTP_TIMEOUT_MS, HttpPolicy
from jiuwenswarm.gateway.extensions_v2.models import HttpFetchInput, HttpFetchOutput

logger = logging.getLogger(__name__)

MAX_REDIRECTS: Final[int] = 3

CODE_HOST_DENIED: Final[str] = "HTTP_HOST_DENIED"
CODE_TIMEOUT: Final[str] = "HTTP_TIMEOUT"
CODE_BODY_TOO_LARGE: Final[str] = "HTTP_BODY_TOO_LARGE"
CODE_REQUEST_FAILED: Final[str] = "HTTP_REQUEST_FAILED"

#: Headers that must never travel to a different host through a redirect.
CREDENTIAL_HEADERS: Final[tuple[str, ...]] = ("authorization", "cookie", "proxy-authorization")

#: Status codes that turn a non-idempotent request into a body-less GET.
GET_REDIRECT_CODES: Final[frozenset[int]] = frozenset({301, 302, 303})


def build_http_ops_build_handler(policy: HttpPolicy) -> EventHandler:
    """Build the BUILTIN handler that adds ``ops.http.fetch``."""

    async def http_fetch(ops_view: EventHandlerOps, request: HttpFetchInput) -> HttpFetchOutput:
        del ops_view
        if not isinstance(request, HttpFetchInput):
            raise TypeError("ops.http.fetch requires HttpFetchInput")
        if not policy.host_allowed(request.url):
            return HttpFetchOutput(code=CODE_HOST_DENIED)
        timeout_ms = _timeout_ms(request.timeout_ms, policy.timeout_ms)
        started = time.monotonic()
        try:
            return await _fetch(request, policy, timeout_ms, started)
        except httpx.TimeoutException:
            return HttpFetchOutput(code=CODE_TIMEOUT, elapsed_ms=_elapsed(started))
        except Exception as exc:  # noqa: BLE001 - an outbound failure is a value, not a crash
            logger.warning(
                "[GatewayExtensions] ops.http.fetch failed: host=%s err=%s",
                _host(request.url),
                type(exc).__name__,
            )
            return HttpFetchOutput(code=CODE_REQUEST_FAILED, elapsed_ms=_elapsed(started))

    async def http_ops_build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        del ops
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("http")
        builder.add_method("http", "fetch", http_fetch)
        return builder

    return http_ops_build


async def _fetch(
    request: HttpFetchInput,
    policy: HttpPolicy,
    timeout_ms: int,
    started: float,
) -> HttpFetchOutput:
    url = request.url
    method = (request.method or "GET").upper()
    headers = dict(request.headers) if isinstance(request.headers, Mapping) else {}
    body = _body(request)
    follow = bool(request.follow_redirects or policy.follow_redirects)
    deadline = started + timeout_ms / 1000
    hops = 0
    async with httpx.AsyncClient(
        timeout=timeout_ms / 1000,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return HttpFetchOutput(code=CODE_TIMEOUT, elapsed_ms=_elapsed(started))
            async with client.stream(
                method,
                url,
                headers=headers,
                content=body,
                timeout=remaining,
            ) as response:
                location = response.headers.get("location", "")
                if not follow or not response.is_redirect or not location or hops >= MAX_REDIRECTS:
                    return await _output(response, policy, started)
                next_url = urljoin(url, location)
                if not policy.host_allowed(next_url):
                    return HttpFetchOutput(
                        status=response.status_code,
                        code=CODE_HOST_DENIED,
                        elapsed_ms=_elapsed(started),
                    )
                if _host(url) != _host(next_url):
                    headers = _without_credentials(headers)
                if response.status_code in GET_REDIRECT_CODES and method not in ("GET", "HEAD"):
                    method = "GET"
                    body = None
                url = next_url
                hops += 1


async def _output(response: httpx.Response, policy: HttpPolicy, started: float) -> HttpFetchOutput:
    headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > policy.max_body_bytes:
            return HttpFetchOutput(
                status=response.status_code,
                headers=headers,
                code=CODE_BODY_TOO_LARGE,
                elapsed_ms=_elapsed(started),
            )
        chunks.append(chunk)
    return HttpFetchOutput(
        status=response.status_code,
        headers=headers,
        body=_decode(b"".join(chunks), headers),
        elapsed_ms=_elapsed(started),
    )


def _without_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in CREDENTIAL_HEADERS}


def _decode(content: bytes, headers: Mapping[str, str]) -> Any:
    text = content.decode("utf-8", errors="replace")
    if "json" not in headers.get("content-type", ""):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _body(request: HttpFetchInput) -> bytes | None:
    if (request.method or "GET").upper() in ("GET", "HEAD"):
        return None
    if request.body is None:
        return None
    if isinstance(request.body, (str, bytes)):
        return request.body.encode("utf-8") if isinstance(request.body, str) else request.body
    return json.dumps(
        dict(request.body) if isinstance(request.body, Mapping) else request.body
    ).encode("utf-8")


def _timeout_ms(requested: object, default: int) -> int:
    value = requested if type(requested) is int and requested > 0 else default
    return min(value, MAX_HTTP_TIMEOUT_MS)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
