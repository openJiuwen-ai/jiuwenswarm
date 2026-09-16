# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-side A4P RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from a4p.errors import A4PProtocolError

from jiuwenswarm.agents.harness.common.a4p_execution_context import (
    authorizer_route_from_request,
)
from jiuwenswarm.agents.harness.common.a4p_runtime import (
    get_a4p_config,
    get_a4p_runtime,
    is_a4p_enabled,
    reconfigure_a4p_runtime,
)
from jiuwenswarm.common.config import update_a4p_in_config
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod


_A4P_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.A4P_AUTHORIZATION_COMPLETE,
        ReqMethod.A4P_AUTHORIZATION_REJECT,
        ReqMethod.A4P_AUTHORIZATION_PENDING,
        ReqMethod.A4P_CONFIG_GET,
        ReqMethod.A4P_CONFIG_UPDATE,
        ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_OPTIONS,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_VERIFY,
    }
)
# Management remains available while disabled so users can configure A4P
# and prepare Passkeys. All management calls require an interactive Web route.
_A4P_MANAGEMENT_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.A4P_CONFIG_GET,
        ReqMethod.A4P_CONFIG_UPDATE,
        ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_OPTIONS,
        ReqMethod.A4P_WEBAUTHN_REGISTRATION_VERIFY,
    }
)


_CONFIG_LOCK = asyncio.Lock()
_CONFIG_FIELDS = frozenset({"enabled", "require_user_signature"})


async def _dispatch_config(request: AgentRequest) -> AgentResponse:
    params = request.params
    if not isinstance(params, dict):
        return _err(request, "params must be object")
    updates = {key: value for key, value in params.items() if key != "session_id"}
    if request.req_method == ReqMethod.A4P_CONFIG_UPDATE:
        if not updates or updates.keys() - _CONFIG_FIELDS:
            return _err(request, "Provide enabled and/or require_user_signature only")
        if any(type(value) is not bool for value in updates.values()):
            return _err(request, "A4P config values must be booleans")
    elif updates:
        return _err(request, "a4p.config.get accepts no config fields")
    async with _CONFIG_LOCK:
        try:
            if request.req_method == ReqMethod.A4P_CONFIG_UPDATE:
                update_a4p_in_config(updates)
                try:
                    await reconfigure_a4p_runtime()
                except Exception as exc:  # noqa: BLE001
                    return _err(
                        request,
                        f"A4P config saved but runtime reconfiguration failed: {exc}",
                        code="A4P_CONFIG_APPLY_FAILED",
                    )
            config = get_a4p_config()
            return _ok(request, {key: bool(config.get(key, False)) for key in _CONFIG_FIELDS})
        except Exception as exc:  # noqa: BLE001
            return _err(request, str(exc), code="INTERNAL_ERROR")


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

    if method in {ReqMethod.A4P_CONFIG_GET, ReqMethod.A4P_CONFIG_UPDATE}:
        if not isinstance(request.params, dict):
            return _err(request, "params must be object")
        if authorizer_route_from_request(request) is None:
            return _err(
                request, "A4P config RPC requires an interactive Web route",
                code="A4P_WEB_SESSION_REQUIRED",
            )
        return await _dispatch_config(request)

    enabled = is_a4p_enabled()
    if not enabled and method == ReqMethod.A4P_AUTHORIZATION_PENDING:
        return _ok(request, {"ok": True, "pending": None})
    if not enabled and method not in _A4P_MANAGEMENT_METHODS:
        return _err(request, "A4P is disabled", code="A4P_DISABLED")
    route = authorizer_route_from_request(request)
    if route is None:
        return _err(
            request,
            "A4P authorization RPC requires an interactive Web route",
            code="A4P_WEB_SESSION_REQUIRED",
        )

    try:
        runtime = get_a4p_runtime()
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
