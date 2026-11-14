# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Agent schema compatibility exports.

Agent request and response models come from the protocol package. ``ReqMethod``
is owned by JiuwenSwarm in ``common.schema.req_method``.
"""

from __future__ import annotations

from e2a.agent_models import (  # noqa: F401
    AgentRef,
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)

from jiuwenswarm.common.schema.req_method import ReqMethod

__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
    "ReqMethod",
]
