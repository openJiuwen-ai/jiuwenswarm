# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Non-streaming controls for an existing invocation, never a chat turn."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import _uses_tenant_pool
from jiuwenswarm.server.runtime.agent_adapter.steering import rejected
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


def _validate(params: dict[str, Any], *, query: bool) -> str | None:
    allowed = {"session_id", "invocation_id", "active_request_id", "input_id"}
    if not query:
        allowed.update({"client_message_id", "content"})
    if set(params) & {"attachments", "files", "images", "media_items", "documents"}:
        return "CONTENT_UNSUPPORTED"
    if set(params) - allowed:
        return "INVALID_REQUEST"
    required = ["session_id", "invocation_id", "active_request_id"]
    if not query:
        required += ["input_id", "client_message_id"]
    for name in required:
        value = params.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            return "INVALID_REQUEST"
    if "input_id" in params and (
        not isinstance(params["input_id"], str) or len(params["input_id"]) > 512
    ):
        return "INVALID_REQUEST"
    if not query:
        content = params.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 32000:
            return "CONTENT_UNSUPPORTED"
    return None


async def handle_chat_steering(ctx: RequestContext) -> None:
    request = ctx.request
    query = request.req_method == ReqMethod.CHAT_STEER_STATUS
    params = request.params if isinstance(request.params, dict) else {}
    input_id = params.get("input_id", "")
    reason = _validate(params, query=query)
    if request.is_stream:
        reason = "INVALID_REQUEST"
    if request.session_id and request.session_id != params.get("session_id"):
        reason = "INVALID_REQUEST"
    result: dict[str, Any]
    if reason:
        result = rejected(reason, input_id=input_id)
    else:
        # Some wire transports only carry the session in params. No lookup may
        # allocate a tenant, agent, session, output lease, or team runtime.
        request.session_id = params["session_id"]
        manager = ctx.services.agent_manager
        if _uses_tenant_pool(request):
            pool = ctx.services.tenant_pool()
            manager = await pool.get_cached_agent_manager(
                *TenantAgentPool.extract_ids(request)
            )
        owner = None
        if manager is not None:
            for candidate in manager.iter_jiuwenswarm_instances():
                check = getattr(candidate, "owns_steering_request", None)
                if callable(check) and check(request):
                    owner = candidate
                    break
        if owner is None:
            if query and not input_id:
                result = {"supported": False, "reason": "RUN_NOT_ACTIVE"}
            elif query:
                result = {"input_id": input_id, "status": "unknown"}
            else:
                result = rejected("RUN_NOT_ACTIVE", input_id=input_id)
        else:
            result = await owner.process_steering(request, query=query)
    payload = {
        **result,
        "input_id": input_id,
        "invocation_id": params.get("invocation_id", ""),
        # Never expose the team's private per-round handle as the wire binding.
        "active_request_id": params.get("active_request_id", ""),
    }
    if result.get("status") == "not_applied":
        # Keep the established E2A classification separate from semantic reasons.
        payload["error_code"] = "E2A.AGENT_ERROR"
        payload["code"] = result.get("reason", "RUN_NOT_ACTIVE")
    response = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=result.get("status") != "not_applied" or (query and not reason),
        payload=payload,
        metadata=request.metadata,
        agent_ref=request.agent_ref,
    )
    await ctx.sink.send_wire(
        encode_agent_response_for_wire(response, response_id=request.request_id)
    )
