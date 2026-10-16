# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 上没有单独网址的方法，从路径里的方法名调用同一批处理函数。"""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import http_chat
from jiuwenswarm.server.context import RequestContext

_CHAT_METHODS = {ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME, ReqMethod.CHAT_ANSWER}


async def handle_http_rpc(ctx: RequestContext, *, is_stream: bool) -> None:
    """聊天走聊天适配。表里有的直接调用。剩下的走默认的一次回包或流式回包。"""
    if ctx.request.req_method in _CHAT_METHODS:
        await http_chat.handle_http_chat(ctx)
        return
    from jiuwenswarm.server.dispatch import try_dispatch

    if await try_dispatch(ctx):
        return
    from jiuwenswarm.server.handlers import _default as chat_default

    if is_stream:
        await chat_default.handle_stream(ctx)
    else:
        await chat_default.handle_unary(ctx)
