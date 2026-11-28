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
