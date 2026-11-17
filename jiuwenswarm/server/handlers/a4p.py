# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A4P HTTP 入口。a4p.* 这一组请求共用一个函数，再转进 package 实现。"""

from __future__ import annotations

from jiuwenswarm.server.context import RequestContext


async def handle_a4p_request(ctx: RequestContext) -> None:
    """处理网页授权那边转发来的 a4p 请求。"""
    from jiuwenswarm.agents.harness.common.a4p.rpc import dispatch_a4p_request

    resp = await dispatch_a4p_request(ctx.request)
    await ctx.sink.send_response(resp)
