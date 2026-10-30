# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUIDispatchAdapter: turn a ``webui.dispatch`` request into a ui.* event."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_NO_CONTRIBUTION,
    CODE_SCHEMA_ERROR,
    CODE_SESSION_MISMATCH,
    CODE_WEBUI_DISABLED,
    UPSTREAM_EVENTS,
)
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.extensions_v2.webui.schema import validate_target
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)


class WebUIDispatchAdapter(GatewayAdapter):
    """The only upstream entry for browser ui.* events."""

    methods: frozenset[str] = frozenset({ReqMethod.WEBUI_DISPATCH.value})

    async def handle(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

        runtime = get_webui_runtime()
        if runtime is None:
            return build_error_response(request, "WebUI extension is not installed", code=CODE_WEBUI_DISABLED)

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
        if set(raw_target) - {"session_id", "slot_id", "package_id", "instance_key"}:
            return build_error_response(request, "unknown target fields", code=CODE_SCHEMA_ERROR)
        if ((request.session_id and session_id != request.session_id)
                or ("session_id" in raw_target and raw_target["session_id"] != session_id)):
            return build_error_response(request, "session_id mismatch", code=CODE_SESSION_MISMATCH)

        target = RenderTarget(
            session_id=session_id,
            slot_id=raw_target.get("slot_id", ""),
            package_id=raw_target.get("package_id", ""),
            instance_key=raw_target.get("instance_key", ""),
        )
        report = validate_target(target)
        if not report.ok:
            return build_error_response(request, report.message, code=report.code or CODE_SCHEMA_ERROR)

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
