"""Thin httpx wrappers — the SDK's only network exit.

``HTTPTransport`` and ``AsyncHTTPTransport`` expose a single ``request`` entry
point. 2xx responses are returned as raw ``httpx.Response`` (so callers can
inspect Content-Type etc.); 4xx/5xx and transport exceptions are translated to
``A2XError`` subclasses via the shared ``_wrap_http_error`` / ``_wrap_transport_error``
helpers so sync and async paths stay in lockstep.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from .errors import (
    A2XConnectionError,
    A2XHTTPError,
    NotFoundError,
    ServerError,
    UserConfigServiceImmutableError,
    ValidationError,
)

HTTPMethod = Literal["GET", "POST", "PUT", "DELETE"]


def _parse_payload(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {"detail": data}


def _wrap_http_error(resp: httpx.Response) -> A2XHTTPError:
    """Map a non-2xx response to the right ``A2XHTTPError`` subclass."""
    payload = _parse_payload(resp)
    detail = ""
    if payload is not None:
        raw_detail = payload.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) else ""
    status = resp.status_code
    message = f"HTTP {status}: {detail or resp.reason_phrase or 'request failed'}"

    if status == 404:
        return NotFoundError(message, status_code=status, payload=payload)
    if status in (400, 422):
        if "user_config" in detail:
            return UserConfigServiceImmutableError(
                message, status_code=status, payload=payload
            )
        return ValidationError(message, status_code=status, payload=payload)
    if 500 <= status < 600:
        return ServerError(message, status_code=status, payload=payload)
    return A2XHTTPError(message, status_code=status, payload=payload)


def _wrap_transport_error(exc: Exception) -> A2XConnectionError:
    return A2XConnectionError(f"{type(exc).__name__}: {exc}")


class HTTPTransport:
    """Synchronous HTTP transport backed by ``httpx.Client``."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            resp = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise _wrap_transport_error(exc) from exc
        if resp.status_code >= 400:
            raise _wrap_http_error(resp)
        return resp

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HTTPTransport":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class AsyncHTTPTransport:
    """Asynchronous HTTP transport backed by ``httpx.AsyncClient``."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)

    async def request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise _wrap_transport_error(exc) from exc
        if resp.status_code >= 400:
            raise _wrap_http_error(resp)
        return resp

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHTTPTransport":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()
