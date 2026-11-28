# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer REST 入口捕获 W3C trace 头并入 channel_context 的行为测试。

背景：REST 入口重建 channel_context 时只含路由身份子集，来自请求头的
traceparent/tracestate 被丢弃，``bind_incoming_request`` 的 extract 接不到
上游上下文，SDK run span 另起新 trace。修复后入口经 ``capture_w3c_headers``
捕获、``_build_channel_context`` 并入；请求未携带 W3C 头时不引入新键
（关闭态无副作用），且空捕获显式重置上下文，避免跨请求残留。
"""

from __future__ import annotations

from jiuwenswarm.server.agent_http_server import (
    _build_channel_context,
    capture_w3c_headers,
)


def test_captured_w3c_headers_merge_into_channel_context():
    capture_w3c_headers({"traceparent": "00-aaa-bbb-01", "tracestate": "es=1"})
    ctx = _build_channel_context({"user_id": "u1"}, None)
    assert ctx["traceparent"] == "00-aaa-bbb-01"
    assert ctx["tracestate"] == "es=1"
    assert ctx.get("user_id") == "u1"


def test_empty_capture_resets_and_leaves_context_clean():
    """无 trace 头（含显式重置）时 channel_context 不新增键，无跨请求残留。"""
    capture_w3c_headers({"traceparent": "00-aaa-bbb-01"})
    capture_w3c_headers({})
    ctx = _build_channel_context({"user_id": "u1"}, None)
    assert "traceparent" not in ctx
    assert "tracestate" not in ctx


def test_request_context_to_agent_request_carries_w3c_header():
    """集成链：真实 request_context 捕获 → build_agent_request 落入 metadata。

    覆盖真实调用时序（request_context 先于 build_agent_request 执行、
    ContextVar 经由真实 starlette Headers 传递），防止未来重构把捕获
    挪出入口路径后纯函数测试无法发现失效。
    """
    from types import SimpleNamespace

    from starlette.datastructures import Headers

    from jiuwenswarm.server.agent_http_routes import request_context
    from jiuwenswarm.server.agent_http_server import build_agent_request

    request = SimpleNamespace(
        headers=Headers({
            "traceparent": "00-aaa-bbb-01",
            "x-user-id": "u1",
            "x-channel-id": "web",
        }),
        path_params={},
    )
    ctx = request_context(request)
    agent_request = build_agent_request(
        method="chat.send",
        params={"query": "hi"},
        request_id=ctx.request_id,
        session_id="sess-w3c-2",
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        is_stream=True,
        routing=ctx.routing,
        tenant_ids=ctx.tenant_ids,
        request_ext=ctx.request_ext,
    )
    assert agent_request.metadata["traceparent"] == "00-aaa-bbb-01"
