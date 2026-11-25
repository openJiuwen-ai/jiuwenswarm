# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 信封转换：应用 Gateway 顶层 ``traceparent`` 优先级约定。

转换逻辑由 ``e2a.agent_compat`` 提供，本模块返回使用本地方法枚举的请求。
顶层 ``traceparent`` 是 Gateway 注入的权威来源，非空时覆盖
``channel_context`` 中的同名值；顶层缺失时保留 channel 值。
"""

from __future__ import annotations

from dataclasses import replace

from e2a import agent_compat as protocol_compat
from e2a.models import E2AEnvelope

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def e2a_to_agent_request(env: E2AEnvelope) -> AgentRequest:
    """Convert an E2A envelope, applying Gateway traceparent precedence."""
    if env.traceparent:
        channel_context = dict(env.channel_context or {})
        channel_context["traceparent"] = env.traceparent
        env = replace(env, channel_context=channel_context)
    request = protocol_compat.e2a_to_agent_request(env)
    method = env.method
    if method:
        try:
            method = ReqMethod(method)
        except ValueError:
            pass  # Preserve extension names; Front validates them before dispatch.
    return AgentRequest(
        request_id=request.request_id, channel_id=request.channel_id, session_id=request.session_id,
        chat_id=request.chat_id, req_method=method, params=request.params, is_stream=request.is_stream,
        timestamp=request.timestamp, metadata=request.metadata, agent_ref=request.agent_ref,
        user_id=request.user_id,
    )


__all__ = ["e2a_to_agent_request"]
