# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把 AgentServer 已有的用户业务适配器挂到 HTTP 专用网址上。

项目列表这类方法本来就由 AgentServer 的适配器处理，WebSocket 入口直接调用。
企业版南向是 HTTP，所以同一份适配器也要从专用网址进来。
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_BOOL_PARAMS = ("include_hidden", "pinned")


def _coerce_bool_params(params: dict) -> None:
    """查询串里的 true/false 还原成布尔。JSON 正文里的布尔保持原样。"""
    for key in _BOOL_PARAMS:
        value = params.get(key)
        if not isinstance(value, str):
            continue
        lowered = value.strip().lower()
        if lowered in {"true", "1"}:
            params[key] = True
        elif lowered in {"false", "0"}:
            params[key] = False


async def handle_gateway_adapter(ctx: RequestContext) -> None:
    """按请求方法找到当前进程里的适配器并回写结果。"""
    request = ctx.request
    method = request.req_method.value if request.req_method is not None else ""
    if isinstance(request.params, dict):
        _coerce_bool_params(request.params)
    registry = ctx.services.adapter_registry
    adapter = registry.get(method) if registry is not None else None
    if adapter is None:
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "code": "HTTP_METHOD_NOT_HANDLED",
                    "error": f"这个方法还没有 HTTP 处理: {method}",
                },
                metadata=request.metadata,
            )
        )
        return
    try:
        response = await adapter.handle(request)
    except Exception as exc:
        logger.exception("[AgentHTTPServer] gateway adapter request failed")
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )
    if method == "session.get_metadata" and response.ok and isinstance(response.payload, dict):
        service = ctx.services.session_message_service
        if service is not None:
            params = request.params if isinstance(request.params, dict) else {}
            session_id = str(params.get("session_id") or request.session_id or "")
            try:
                response.payload["queued_session_messages"] = await service.queued_for_target(
                    session_id, request.user_id
                )
            except Exception:
                logger.warning(
                    "[AgentHTTPServer] queue snapshot failed: session_id=%s",
                    session_id,
                    exc_info=True,
                )
    await ctx.sink.send_response(response)
