# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 聊天路由上的薄适配。

只在聊天请求上调用：先确认会话还能收新聊天，再让扩展看输入，再抄已有团队名字，
再记隐式反馈，然后调用和 WebSocket 同一份一次性回包或流式回包。
"""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers import _default as chat_default
from jiuwenswarm.server.middleware.chat import (
    ensure_auto_team_binding,
    trigger_before_chat_request,
    try_record_implicit_feedback,
)
from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError, guard

_CHAT_BOOKKEEPING = (
    ReqMethod.CHAT_SEND,
    ReqMethod.CHAT_RESUME,
    ReqMethod.CHAT_ANSWER,
)


def is_http_chat_method(method: ReqMethod | None) -> bool:
    """这三条聊天不在普通处理函数表里，网址直接交给聊天适配。"""
    return method in _CHAT_BOOKKEEPING


async def handle_http_chat(ctx: RequestContext) -> None:
    """Bind in-turn tool delivery to this HTTP stream, never another caller."""
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.runtime.host_services import scoped_runtime_push_handler

    request = ctx.request
    active = True

    async def push(message: dict) -> bool:
        same_request = (
            message.get("session_id") == request.session_id
            and message.get("channel_id") == request.channel_id
            and message.get("request_id") == request.request_id
        )
        is_file_event = message.get("payload", {}).get("event_type") == "chat.file"
        stream_ready = active and request.is_stream
        if not stream_ready or not same_request or not is_file_event:
            return False
        return await ctx.sink.send_chunk(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=message["payload"],
                metadata=message.get("metadata") or request.metadata,
                is_complete=False,
            ),
            sequence=0,
        )

    try:
        with scoped_runtime_push_handler(
            {
                "request_id": request.request_id,
                "channel_id": request.channel_id,
                "session_id": request.session_id,
                "metadata": request.metadata,
            },
            push,
        ):
            await _handle_http_chat(ctx)
    finally:
        active = False


async def _handle_http_chat(ctx: RequestContext) -> None:
    """先拦住归档或删除中的会话，再跑聊天前的三步，然后回包。"""
    request = ctx.request
    if await _reject_closed_session(ctx):
        return
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


async def _reject_closed_session(ctx: RequestContext) -> bool:
    """会话正在归档、已经归档或正在删除时，拒绝这轮聊天。

    错误码与 WebSocket 入口的会话守卫相同。检查放在登记聊天和聊天前步骤之前，
    避免归档过程中又写下新内容。
    """
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    try:
        guard(
            str(params.get("session_id") or request.session_id or ""),
            str(params.get("project_id") or ""),
        )
    except LifecycleError as exc:
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"code": exc.code, "error": str(exc)},
                metadata=request.metadata,
            )
        )
        return True
    return False
