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
        event_name = str(params.get("event") or "").strip()
        if event_name not in UPSTREAM_EVENTS:
            return build_error_response(request, f"unsupported webui event: {event_name}", code=CODE_SCHEMA_ERROR)

        session_id = str(params.get("session_id") or request.session_id or "").strip()
        if request.session_id and session_id != request.session_id:
            return build_error_response(request, "session_id mismatch", code=CODE_SESSION_MISMATCH)

        raw_target = params.get("target")
        target_fields = raw_target if isinstance(raw_target, dict) else {}
        target = RenderTarget(
            session_id=session_id,
            slot_id=str(target_fields.get("slot_id") or ""),
            package_id=str(target_fields.get("package_id") or ""),
            instance_key=str(target_fields.get("instance_key") or ""),
        )
        report = validate_target(target)
        if not report.ok:
            return build_error_response(request, report.message, code=report.code or CODE_SCHEMA_ERROR)

        if not runtime.catalog().current().allows_slot(target.slot_id):
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

        payload = params.get("payload")
        action = UiActionInput(
            event_type=event_name,
            target=target,
            payload=payload if isinstance(payload, dict) else {},
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
