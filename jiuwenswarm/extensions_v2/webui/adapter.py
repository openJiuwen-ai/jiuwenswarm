# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUIDispatchAdapter: turn a ``webui.dispatch`` request into a ui.* event."""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.webui_resources import encode_resource_route
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
        ReqMethod.WEBUI_SETTINGS_CATALOG.value,
        ReqMethod.WEBUI_RESOURCE_READ.value,
    })

    async def handle(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

        runtime = get_webui_runtime()
        if runtime is None:
            return build_error_response(request, "WebUI extension is not installed", code=CODE_WEBUI_DISABLED)

        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        if request.req_method == ReqMethod.WEBUI_RESOURCE_READ:
            params = request.params
            if (request.channel_id != DEFAULT_CHANNEL_ID or not isinstance(params, dict)
                    or set(params) != {"resource_id", "path", "connection_id"}
                    or any(not isinstance(value, str) for value in params.values())):
                return build_error_response(request, "Invalid WebUI resource request", code=CODE_SCHEMA_ERROR)
            route = encode_resource_route(request.user_id or "", request.session_id or "", params["connection_id"])
            try:
                data, media_type = await runtime.resources.read(params["resource_id"], params["path"], route=route)
            except (ValueError, FileNotFoundError):
                return build_error_response(request, "WebUI resource not found", code="NOT_FOUND")
            return AgentResponse(
                request_id=request.request_id, channel_id=request.channel_id, ok=True,
                payload={"data": base64.b64encode(data).decode("ascii"), "media_type": media_type},
            )

        trusted_connection = str(metadata.get("ws_id") or "").strip()
        if request.req_method == ReqMethod.WEBUI_SETTINGS_CATALOG:
            if request.channel_id != DEFAULT_CHANNEL_ID or not trusted_connection:
                return build_error_response(request, "web connection required", code=CODE_CONNECTION_REQUIRED)
            if request.params:
                return build_error_response(request, "settings catalog accepts no parameters", code=CODE_SCHEMA_ERROR)
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=True,
                                 payload=runtime.catalog().current().settings_payload(), metadata=request.metadata)
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
        if any(not isinstance(value, str) for value in raw_target.values()):
            return build_error_response(request, "target fields must be strings", code=CODE_SCHEMA_ERROR)
        if set(raw_target) - {"session_id", "slot_id", "package_id", "instance_key", "scope"}:
            return build_error_response(request, "unknown target fields", code=CODE_SCHEMA_ERROR)

        slot_id = raw_target.get("slot_id", "")
        spec = slot_spec(slot_id)
        if spec is None:
            return build_error_response(request, f"slot not allowed: {slot_id}", code=CODE_SCHEMA_ERROR)
        declared_scope = raw_target.get("scope", "").strip()
        if declared_scope and not spec.accepts_scope(declared_scope):
            return build_error_response(request, "target.scope does not match the slot", code=CODE_SCHEMA_ERROR)
        resolved_scope = declared_scope or spec.scope.value
        if resolved_scope == SlotScope.APPLICATION.value:
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
                package_id=raw_target.get("package_id", ""),
                instance_key=raw_target.get("instance_key", ""),
                scope=declared_scope,
            )
            target, report = apply_slot_scope(target, trusted_connection)
        else:
            raw_session_id = params.get("session_id", request.session_id or "")
            if not isinstance(raw_session_id, str):
                return build_error_response(request, "target fields must be strings", code=CODE_SCHEMA_ERROR)
            session_id = raw_session_id or (request.session_id or "")
            if (
                (request.session_id and session_id != request.session_id)
                or ("session_id" in raw_target and raw_target["session_id"] != session_id)
            ):
                return build_error_response(request, "session_id mismatch", code=CODE_SESSION_MISMATCH)
            target = RenderTarget(
                session_id=session_id,
                slot_id=slot_id,
                package_id=raw_target.get("package_id", ""),
                instance_key=raw_target.get("instance_key", ""),
                scope=declared_scope,
            )
            target, report = apply_slot_scope(target)
        if not report.ok:
            return build_error_response(request, report.message, code=report.code or CODE_SCHEMA_ERROR)

        if event_name == "ui.ready":
            runtime.remember_ready_client(trusted_connection, target.session_id)
        catalog = runtime.catalog().current()
        if not catalog.allows_target(target.slot_id, target.package_id, instance_key=target.instance_key,
                                     scope=target.resolved_scope):
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
        dispatch_kwargs: dict[str, Any] = {
            "user_id": request.user_id,
            "channel_id": request.channel_id or "",
        }
        if trusted_connection:
            dispatch_kwargs["connection_id"] = trusted_connection
        if event_name == "ui.ready" and not target.package_id and spec.capacity == "list":
            contributions = sorted(
                (item for item in catalog.items if item.type == "web.slot.contribution" and item.slot == target.slot_id
                 and item.settings.get("kind", "item") == "item"
                 and (not item.settings or item.settings["scope"] == target.resolved_scope)),
                key=lambda item: (item.order, item.package_id, item.id),
            )
            package_ids = dict.fromkeys(item.package_id for item in contributions)
            logger.debug("[WebUI] list ready: slot=%s packages=%s", target.slot_id, tuple(package_ids))
            if len(package_ids) == 1:
                action = replace(action, target=replace(target, package_id=next(iter(package_ids))))
            elif package_ids:
                results: list[dict[str, Any]] = []
                for package_id in package_ids:
                    scoped = replace(action, target=replace(target, package_id=package_id))
                    try:
                        output = await runtime.dispatch(scoped, **dispatch_kwargs)
                        receipt = {"package_id": package_id, "accepted": bool(output and output.accepted),
                                   "status": output.status if output else "no_handler"}
                        if output is not None and output.code:
                            receipt["code"] = output.code
                    except Exception:  # noqa: BLE001 - isolate each declared package's initialization
                        logger.exception("[WebUI] list ready failed: target=%s", scoped.target.key)
                        receipt = {"package_id": package_id, "accepted": False,
                                   "status": "error", "code": "INTERNAL_ERROR"}
                    results.append(receipt)
                accepted = all(result["accepted"] for result in results)
                status = "ready" if accepted else "partial" if any(result["accepted"] for result in results) else "rejected"
                return AgentResponse(
                    request_id=request.request_id, channel_id=request.channel_id, ok=True,
                    payload={"event": event_name, "accepted": accepted, "status": status, "results": results},
                    metadata=request.metadata,
                )
        try:
            output = await runtime.dispatch(action, **dispatch_kwargs)
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
