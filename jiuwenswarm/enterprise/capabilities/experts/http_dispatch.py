# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版把专家包 HTTP 登记到 AgentServer。

个人版 WebSocket 不经过这里。本模块不依赖 Gateway 企业包。
"""

from __future__ import annotations

import logging

from openjiuwen_protocol.expert_http_routes import expert_http_routes

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_BOOL_PARAMS = ("include_team_compatibility",)


def expert_http_route_rows() -> tuple[tuple[str, str, str], ...]:
    """AgentServer 要挂出的 (方法, 动词, 路径)。"""
    return tuple((row.method, row.verb, row.path) for row in expert_http_routes())


def install_expert_http_handlers(handlers: dict) -> None:
    """把专家包方法写进调用方的处理表。重复调用覆盖同一项。"""
    for row in expert_http_routes():
        handlers[ReqMethod(row.method)] = handle_agent_templates_request


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


def _method_name(request) -> str:
    method = getattr(request, "req_method", None)
    value = getattr(method, "value", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "agent_templates"


async def handle_agent_templates_request(ctx: RequestContext) -> None:
    """交给当前进程的 Agent 门面，和 WebSocket 使用同一份专家包处理。"""
    request = ctx.request
    if isinstance(request.params, dict):
        _coerce_bool_params(request.params)
    manager = ctx.services.agent_manager
    method_name = _method_name(request)
    if manager is None:
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "code": "HTTP_METHOD_NOT_HANDLED",
                    "error": f"这个方法还没有 HTTP 处理: {method_name}",
                },
                metadata=request.metadata,
            )
        )
        return
    try:
        response = await manager.process_message(request)
    except Exception as exc:
        logger.exception("[AgentHTTPServer] agent template request failed")
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )
    await ctx.sink.send_response(response)
