# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 信封转换：应用 Gateway 顶层 ``traceparent`` 优先级约定。

转换逻辑由 ``e2a.agent_compat`` 提供；本模块只补一层本仓约定：
顶层 ``traceparent`` 是 Gateway 注入的权威来源，非空时覆盖
``channel_context`` 中的同名值；顶层缺失时保留 channel 值。
"""

from __future__ import annotations

from dataclasses import replace

from e2a import agent_compat as protocol_compat
from e2a.models import E2AEnvelope

from jiuwenswarm.common.schema.agent import AgentRequest


def e2a_to_agent_request(env: E2AEnvelope) -> AgentRequest:
    """Convert an E2A envelope, applying Gateway traceparent precedence."""
    if not env.traceparent:
        return protocol_compat.e2a_to_agent_request(env)
    channel_context = dict(env.channel_context or {})
    channel_context["traceparent"] = env.traceparent
    return protocol_compat.e2a_to_agent_request(replace(env, channel_context=channel_context))


__all__ = ["e2a_to_agent_request"]
