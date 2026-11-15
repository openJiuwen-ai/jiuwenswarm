# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``e2a.agent_models``。

Transport models are re-exported from e2a without method validation.
ReqMethod remains owned by this repository and is resolved at AgentServer ingress.
"""

from __future__ import annotations

from e2a.agent_models import (  # noqa: F401
    AgentRef,
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)
from jiuwenswarm.common.schema.message import ReqMethod

__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
    "ReqMethod",
]
