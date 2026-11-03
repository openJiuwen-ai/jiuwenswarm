# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把 AgentServer 已有的技能处理挂到 HTTP 专用网址上。

技能列表由 JiuWenSwarm 门面里的 SkillManager 处理。WebSocket 走
process_message，企业版南向是 HTTP，所以同一条路径也要从专用网址进来。
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_BOOL_PARAMS = ("with_installed", "refresh_marketplaces")


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


async def handle_skills_request(ctx: RequestContext) -> None:
    """交给当前进程的 Agent 门面，和 WebSocket 使用同一份技能处理。"""
    request = ctx.request
    if isinstance(request.params, dict):
        _coerce_bool_params(request.params)
    manager = ctx.services.agent_manager
    if manager is None:
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "code": "HTTP_METHOD_NOT_HANDLED",
                    "error": f"这个方法还没有 HTTP 处理: {getattr(request.req_method, 'value', 'skills')}",
                },
                metadata=request.metadata,
            )
        )
        return
    try:
        response = await manager.process_message(request)
    except Exception as exc:
        logger.exception("[AgentHTTPServer] skills request failed")
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )
    await ctx.sink.send_response(response)
