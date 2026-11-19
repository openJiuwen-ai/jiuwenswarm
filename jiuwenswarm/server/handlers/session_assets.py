# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把会话素材挂到 AgentServer 的 HTTP 专用网址上。

素材登记由 Agent 门面里的 SessionAssetManager 处理。WebSocket 走
process_message，企业版南向是 HTTP，所以同一条路径也要从专用网址进来。
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


async def handle_session_assets_request(ctx: RequestContext) -> None:
    """交给当前进程的 Agent 门面，和 WebSocket 使用同一份素材处理。"""
    request = ctx.request
    manager = ctx.services.agent_manager
    if manager is None:
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "code": "HTTP_METHOD_NOT_HANDLED",
                    "error": (
                        "这个方法还没有 HTTP 处理: "
                        f"{getattr(request.req_method, 'value', 'session.assets')}"
                    ),
                },
                metadata=request.metadata,
            )
        )
        return
    try:
        response = await manager.process_message(request)
    except Exception as exc:
        logger.exception("[AgentHTTPServer] session assets request failed")
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )
    await ctx.sink.send_response(response)
