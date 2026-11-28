# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""gateway REST 身份头透传 W3C trace 上下文的行为测试。

背景：gateway 的 client span 会把 traceparent 注入 ``channel_context``，
但 ``identity_headers`` 组装 X-* 身份头时不携带 W3C 键，AgentServer 侧
``bind_incoming_request`` 的 extract 拿不到上下文，链路在 REST 边界断开、
跨服务 trace 无法合并。修复后 channel_context 中的 W3C 键随 HTTP 头透传；
未注入（遥测关闭/外部未携带）时输出与改前逐字节等价（关闭态无副作用）。
"""

from __future__ import annotations

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.gateway.routing.agent_rest_map import identity_headers


def _envelope(channel_context: dict) -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-w3c-1",
        session_id="sess-w3c-1",
        channel="web",
        method="chat.send",
        params={"query": "hi"},
        user_id="u1",
        channel_context=channel_context,
    )


def test_identity_headers_passthrough_w3c_keys():
    env = _envelope({"traceparent": "00-aaa-bbb-01", "tracestate": "es=1"})
    headers = identity_headers(env, accept="application/json")
    assert headers["traceparent"] == "00-aaa-bbb-01"
    assert headers["tracestate"] == "es=1"


def test_identity_headers_absent_w3c_context_stays_clean():
    """关闭态无副作用：channel_context 无 W3C 键时不新增任何 trace 头。"""
    env = _envelope({})
    headers = identity_headers(env, accept="application/json")
    assert "traceparent" not in headers
    assert "tracestate" not in headers
