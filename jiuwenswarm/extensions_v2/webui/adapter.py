# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUIDispatchAdapter: turn a ``webui.dispatch`` request into a ui.* event."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_CHANNEL_UNSUPPORTED,
    CODE_CONNECTION_REQUIRED,
    CODE_NO_CONTRIBUTION,
    CODE_SCHEMA_ERROR,
    CODE_SESSION_MISMATCH,
    CODE_WEBUI_DISABLED,
    DEFAULT_CHANNEL_ID,
    UPSTREAM_EVENTS,
    SlotScope,
    slot_spec,
)
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.extensions_v2.webui.schema import apply_slot_scope
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)


class WebUIDispatchAdapter(GatewayAdapter):
    """The only upstream entry for browser ui.* events."""

    methods: frozenset[str] = frozenset({
        ReqMethod.WEBUI_DISPATCH.value,
        ReqMethod.WEBUI_CONNECTION_CLOSED.value,
    })

    async def handle(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

        runtime = get_webui_runtime()
        if runtime is None:
            return build_error_response(request, "WebUI extension is not installed", code=CODE_WEBUI_DISABLED)

        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        trusted_connection = str(metadata.get("ws_id") or "").strip()
        if request.req_method == ReqMethod.WEBUI_CONNECTION_CLOSED:
            if request.channel_id != DEFAULT_CHANNEL_ID:
                return build_error_response(request, "web connection required", code=CODE_CHANNEL_UNSUPPORTED)
            if not trusted_connection:
                return build_error_response(request, "connection_id is required", code=CODE_CONNECTION_REQUIRED)
            await runtime.drop_connection(trusted_connection)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"status": "closed"},
            )

        params = request.params if isinstance(request.params, dict) else {}
        event_name = params.get("event")
        if not isinstance(event_name, str) or event_name not in UPSTREAM_EVENTS:
            return build_error_response(request, f"unsupported webui event: {event_name}", code=CODE_SCHEMA_ERROR)

        raw_target = params.get("target")
        payload = params.get("payload", {})
        if not isinstance(raw_target, dict) or not isinstance(payload, dict):
            return build_error_response(request, "target and payload must be objects", code=CODE_SCHEMA_ERROR)
        session_id = params.get("session_id", request.session_id or "")
        if not isinstance(session_id, str) or any(not isinstance(value, str) for value in raw_target.values()):
            return build_error_response(request, "target fields must be strings", code=CODE_SCHEMA_ERROR)
        if set(raw_target) - {"session_id", "slot_id", "package_id", "instance_key", "scope"}:
            return build_error_response(request, "unknown target fields", code=CODE_SCHEMA_ERROR)
        target_fields = raw_target if isinstance(raw_target, dict) else {}
        slot_id = str(target_fields.get("slot_id") or "")
        spec = slot_spec(slot_id)
        if spec is None:
            return build_error_response(request, f"slot not allowed: {slot_id}", code=CODE_SCHEMA_ERROR)
        declared_scope = str(target_fields.get("scope") or "").strip()
        if spec.scope is SlotScope.APPLICATION:
            channel_id = str(request.channel_id or "").strip()
            if channel_id and channel_id != DEFAULT_CHANNEL_ID:
                return build_error_response(
                    request,
                    "application slots are delivered only on the web channel",
                    code=CODE_CHANNEL_UNSUPPORTED,
                )
            target = RenderTarget(
                session_id="",
                slot_id=slot_id,
                package_id=str(target_fields.get("package_id") or ""),
                instance_key=str(target_fields.get("instance_key") or ""),
                scope=declared_scope,
            )
            target, report = apply_slot_scope(target, trusted_connection)
        else:
            session_id = str(params.get("session_id") or request.session_id or "").strip()
            if ((request.session_id and session_id != request.session_id)
                    or ("session_id" in raw_target and raw_target["session_id"] != session_id)):
                return build_error_response(request, "session_id mismatch", code=CODE_SESSION_MISMATCH)
            target = RenderTarget(
                session_id=session_id,
                slot_id=slot_id,
                package_id=str(target_fields.get("package_id") or ""),
                instance_key=str(target_fields.get("instance_key") or ""),
                scope=declared_scope,
            )
            target, report = apply_slot_scope(target)
        if not report.ok:
            return build_error_response(request, report.message, code=report.code or CODE_SCHEMA_ERROR)

        if event_name == "ui.ready":
            runtime.remember_ready_client(trusted_connection, target.session_id)
        if not runtime.catalog().current().allows_target(target.slot_id, target.package_id):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "event": event_name,
                    "accepted": False,
                    "status": "no_contribution",
                    "code": CODE_NO_CONTRIBUTION,
                },
                metadata=request.metadata,
            )

        action = UiActionInput(
            event_type=event_name,
            target=target,
            payload=payload,
        )
        try:
            output = await runtime.dispatch(
                action,
                user_id=request.user_id,
                channel_id=request.channel_id or "",
                connection_id=trusted_connection,
            )
        except Exception as exc:  # noqa: BLE001 - map event-chain failures to a response
            logger.exception("[WebUI] upstream event failed: event=%s target=%s", event_name, target.key)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")

        result: dict[str, Any] = {
            "event": event_name,
            "accepted": bool(output.accepted) if output is not None else False,
            "status": output.status if output is not None else "no_handler",
        }
        if output is not None and output.code:
            result["code"] = output.code
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result,
            metadata=request.metadata,
        )
