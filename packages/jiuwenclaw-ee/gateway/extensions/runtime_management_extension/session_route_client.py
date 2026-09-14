# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime Manager 会话路由客户端：``route`` / ``touch``，不发业务消息。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.security.link_mtls import LinkMTLSConfig

logger = logging.getLogger(__name__)

# HLD §3.1：过载可重试；CONFIG_NOT_FOUND 虽是 503 但不可重试。
_RETRYABLE_CODES = frozenset(
    {"SCOPE_QUEUE_FULL", "SCOPE_FULL_TIMEOUT", "NO_POD_AVAILABLE", "TRANSPORT"}
)
_FATAL_CODES = frozenset({"VALIDATION", "CONFIG_NOT_FOUND"})


@dataclass(frozen=True)
class RouteResult:
    pod_sse_url: str
    pod_id: str
    request_id: str
    mtls_deployment_id: str = ""
    mtls_binding_id: str = ""
    mtls_binding_epoch: int = 0


class RouteError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.status_code = status_code


class RetryableRouteError(RouteError):
    """编排可用同一 ``request_id`` 再调 ``route``。"""


class FatalRouteError(RouteError):
    """参数错 / 无配置，不要重试。"""


class RuntimeSessionRouteClient:
    """``POST /api/session/route``、``POST /api/session/touch``。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        http_client: httpx.AsyncClient | None = None,
        link_mtls_config: LinkMTLSConfig | None = None,
    ) -> None:
        root = (
            (
                base_url
                or read_env("GATEWAY_RUNTIME_MANAGER_URL", "http://127.0.0.1:8091")
            )
            .strip()
            .rstrip("/")
        )
        if not root:
            root = "http://127.0.0.1:8091"
        self._link_mtls = link_mtls_config or LinkMTLSConfig.from_env()
        if (
            self._link_mtls.profile is not None
            and base_url is None
            and not read_env("GATEWAY_RUNTIME_MANAGER_URL", "")
        ):
            root = "link://runtime"
        root = self._link_mtls.resolve_endpoint(root, role="runtime")
        self._route_url = f"{root}/api/session/route"
        self._touch_url = f"{root}/api/session/touch"
        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            timeout = timeout_seconds
            if timeout is None:
                try:
                    timeout = float(read_env("GATEWAY_RUNTIME_MANAGER_TIMEOUT", "40"))
                except (TypeError, ValueError):
                    timeout = 40.0
            if timeout <= 0:
                timeout = 40.0
            # route 可能排队到 scope_full_timeout（默认 30s），连接超时单独收短
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
                follow_redirects=False,
                trust_env=False,
                **self._link_mtls.client_kwargs(role="runtime"),
            )
            self._owns_http = True

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "RuntimeSessionRouteClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def route(
        self,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        request_id: str,
        user_id: str | None = None,
    ) -> RouteResult:
        session_id = (session_id or "").strip()
        group_id = (group_id or "").strip()
        bot_id = (bot_id or "").strip()
        request_id = (request_id or "").strip()
        user_id = (user_id or "").strip() or None
        if not (session_id and group_id and bot_id and request_id):
            raise FatalRouteError(
                "route requires session_id/group_id/bot_id/request_id",
                code="VALIDATION",
            )

        metadata = {
            "request_id": request_id,
            "session_id": session_id,
            "bot_id": bot_id,
            "extra": {"group_id": group_id},
        }
        if user_id:
            metadata["user_id"] = user_id
        body = await self._post(
            self._route_url,
            {"type": "route", "metadata": metadata, "rawdata": {}},
        )
        rawdata = _rawdata(body)
        pod_sse_url = str(rawdata.get("pod_sse_url") or "").strip()
        pod_id = str(rawdata.get("pod_id") or "").strip()
        if not pod_sse_url or not pod_id:
            raise FatalRouteError(
                "route response missing pod_sse_url/pod_id",
                code="VALIDATION",
            )
        logger.debug(
            "[SessionRoute] routed: session=%s pod=%s url=%s request_id=%s",
            session_id,
            pod_id,
            pod_sse_url,
            request_id,
        )
        result = RouteResult(
            pod_sse_url=pod_sse_url,
            pod_id=pod_id,
            request_id=request_id,
            mtls_deployment_id=str(rawdata.get("mtls_deployment_id") or "").strip(),
            mtls_binding_id=str(rawdata.get("mtls_binding_id") or "").strip(),
            mtls_binding_epoch=_positive_int(rawdata.get("mtls_binding_epoch")),
        )
        self._validate_route_binding(result)
        return result

    async def touch(self, *, session_id: str, request_id: str) -> bool:
        """保活。返回 False 表示会话已过期，编排应重新 ``route``。"""
        session_id = (session_id or "").strip()
        request_id = (request_id or "").strip()
        if not (session_id and request_id):
            raise FatalRouteError(
                "touch requires session_id/request_id",
                code="VALIDATION",
            )
        body = await self._post(
            self._touch_url,
            {
                "type": "touch",
                "metadata": {"request_id": request_id, "session_id": session_id},
                "rawdata": {},
            },
        )
        rawdata = _rawdata(body)
        touched = bool(rawdata.get("touched"))
        logger.debug("[SessionRoute] touch: session=%s touched=%s", session_id, touched)
        return touched

    async def _post(self, url: str, envelope: dict) -> dict:
        try:
            response = await self._http.post(
                url,
                json=envelope,
                headers=self._link_mtls.binding_headers(),
            )
        except httpx.RequestError as exc:
            logger.warning("[SessionRoute] transport error: url=%s error=%s", url, exc)
            raise RetryableRouteError(
                f"request failed: {exc}", code="TRANSPORT"
            ) from exc
        body = _json_object(response)
        if response.status_code >= 400 or body.get("ok") is False:
            raise _error_from_response(response, body)
        return body

    def _validate_route_binding(self, result: RouteResult) -> None:
        identity = self._link_mtls.identity
        if not self._link_mtls.enforced or identity is None:
            return
        if (
            result.mtls_deployment_id != identity.mtls_deployment_id
            or result.mtls_binding_id != identity.mtls_binding_id
            or result.mtls_binding_epoch != identity.mtls_binding_epoch
        ):
            raise FatalRouteError(
                "runtime route response link binding mismatch",
                code="LINK_BINDING_MISMATCH",
            )


def _rawdata(body: dict) -> dict:
    raw = body.get("rawdata")
    return raw if isinstance(raw, dict) else body


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _json_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _error_from_response(response: httpx.Response, body: dict) -> RouteError:
    code = str(body.get("error_code") or "").strip()
    if not code:
        code = "TRANSPORT" if response.status_code >= 500 else "VALIDATION"
    message = str(body.get("error_message") or body.get("message") or "").strip()
    if not message:
        message = f"request failed: http={response.status_code} code={code}"
    retry_after = body.get("retry_after")
    if retry_after is None:
        retry_after = response.headers.get("Retry-After")
    try:
        retry_after_int = int(retry_after) if retry_after not in (None, "") else None
    except (TypeError, ValueError):
        retry_after_int = None
    if code in _RETRYABLE_CODES:
        cls = RetryableRouteError
    elif code in _FATAL_CODES:
        cls = FatalRouteError
    elif response.status_code >= 500 or response.status_code == 429:
        cls = RetryableRouteError
    else:
        cls = FatalRouteError
    return cls(
        message,
        code=code,
        retry_after=retry_after_int,
        status_code=response.status_code,
    )
