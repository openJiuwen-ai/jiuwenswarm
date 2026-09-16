# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-side A4P RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from a4p.errors import A4PProtocolError

from jiuwenswarm.agents.harness.common.a4p_execution_context import (
    authorizer_route_from_request,
)
from jiuwenswarm.agents.harness.common.a4p_runtime import get_a4p_runtime, is_a4p_enabled
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod


_A4P_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.A4P_AUTHORIZATION_COMPLETE,
        ReqMethod.A4P_AUTHORIZATION_REJECT,
        ReqMethod.A4P_AUTHORIZATION_PENDING,
        ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_OPTIONS,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_VERIFY,
    }
)
# Intentionally available while disabled so users can prepare Passkeys.
# Management still requires an interactive Web route and WebAuthn verification;
# enabled controls intent authorization, not credential management.
_A4P_MANAGEMENT_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_OPTIONS,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_VERIFY,
    }
)


def get_a4p_req_methods() -> frozenset[ReqMethod]:
    return _A4P_METHODS


def _ok(request: AgentRequest, payload: dict[str, Any]) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


def _err(request: AgentRequest, message: str, *, code: str = "BAD_REQUEST") -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload={"error": message, "code": code},
        metadata=request.metadata,
    )


async def dispatch_a4p_request(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    method = request.req_method

    enabled = is_a4p_enabled()
    if not enabled and method == ReqMethod.A4P_AUTHORIZATION_PENDING:
        return _ok(request, {"ok": True, "pending": None})
    if not enabled and method not in _A4P_MANAGEMENT_METHODS:
        return _err(request, "A4P is disabled", code="A4P_DISABLED")
    runtime = get_a4p_runtime()
    route = authorizer_route_from_request(request)
    if route is None:
        return _err(
            request,
            "A4P authorization RPC requires an interactive Web route",
            code="A4P_WEB_SESSION_REQUIRED",
        )

    try:
        if method == ReqMethod.A4P_AUTHORIZATION_COMPLETE:
            request_id = str(params.get("requestId") or "").strip()
            assertion = params.get("assertion")
            result = runtime.authorizer.complete(
                request_id,
                route,
                assertion=assertion if isinstance(assertion, dict) else None,
            )
            if not result.get("ok"):
                code = str(result.get("code") or "") or (
                    "A4P_ROUTE_MISMATCH"
                    if "route mismatch" in str(result.get("error") or "")
                    else "A4P_NOT_PENDING"
                )
                return _err(request, str(result.get("error") or "request failed"), code=code)
            return _ok(request, result)

        if method == ReqMethod.A4P_AUTHORIZATION_REJECT:
            request_id = str(params.get("requestId") or "").strip()
            result = runtime.authorizer.reject(
                request_id,
                route,
                str(params.get("reason") or ""),
            )
            if not result.get("ok"):
                code = (
                    "A4P_ROUTE_MISMATCH"
                    if "route mismatch" in str(result.get("error") or "")
                    else "A4P_NOT_PENDING"
                )
                return _err(request, str(result.get("error") or "request failed"), code=code)
            return _ok(request, result)

        if method == ReqMethod.A4P_AUTHORIZATION_PENDING:
            return _ok(request, runtime.authorizer.pending_for_route(route))

        if method == ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET:
            return _ok(request, runtime.webauthn_credential_status())

        if method == ReqMethod.A4P_WEBAUTHN_REGISTRATION_OPTIONS:
            return _ok(request, runtime.webauthn_registration_options(route))

        if method == ReqMethod.A4P_WEBAUTHN_REGISTRATION_VERIFY:
            registration_request_id = str(
                params.get("registrationRequestId") or ""
            ).strip()
            credential = params.get("credential")
            if not registration_request_id:
                return _err(
                    request,
                    "registrationRequestId missing",
                    code="A4P_WEBAUTHN_REGISTRATION_INVALID",
                )
            if not isinstance(credential, dict):
                return _err(
                    request,
                    "WebAuthn registration credential missing",
                    code="A4P_WEBAUTHN_REGISTRATION_INVALID",
                )
            return _ok(
                request,
                runtime.verify_webauthn_registration(
                    registration_request_id=registration_request_id,
                    credential=credential,
                    route=route,
                ),
            )
    except A4PProtocolError as exc:
        return _err(request, str(exc), code=exc.code)
    except ValueError as exc:
        return _err(
            request,
            str(exc),
            code="A4P_WEBAUTHN_REGISTRATION_INVALID",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(request, str(exc), code="INTERNAL_ERROR")

    await asyncio.sleep(0)
    return _err(request, "unknown A4P req_method", code="BAD_REQUEST")


__all__ = ["dispatch_a4p_request", "get_a4p_req_methods"]
