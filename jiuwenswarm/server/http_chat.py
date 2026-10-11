# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 聊天路由上的薄适配。

只在聊天请求上调用：先让扩展看输入，再抄已有团队名字，再记隐式反馈，
然后调用和 WebSocket 同一份一次性回包或流式回包。
"""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers import _default as chat_default
from jiuwenswarm.server.middleware.chat import (
    ensure_auto_team_binding,
    trigger_before_chat_request,
    try_record_implicit_feedback,
)

_CHAT_BOOKKEEPING = (
    ReqMethod.CHAT_SEND,
    ReqMethod.CHAT_RESUME,
    ReqMethod.CHAT_ANSWER,
)


async def handle_http_chat(ctx: RequestContext) -> None:
    """跑完聊天前的三步，再进入真正的回包。"""
    request = ctx.request
    pending: tuple[object, str, str] | None = None
    try:
        if request.req_method in _CHAT_BOOKKEEPING and request.session_id:
            runtime = ctx.services.execution_runtime()
            runtime.begin_chat_request(request.session_id, request.request_id)
            pending = (runtime, request.session_id, request.request_id)
        await trigger_before_chat_request(request)
        await ensure_auto_team_binding(request)
        if request.req_method == ReqMethod.CHAT_SEND:
            await try_record_implicit_feedback(request)
        if request.is_stream:
            await chat_default.handle_stream(ctx)
        else:
            await chat_default.handle_unary(ctx)
    finally:
        if pending is not None:
            runtime, session_id, request_id = pending
            runtime.end_chat_request(session_id, request_id)
