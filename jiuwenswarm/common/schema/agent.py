# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Agent 请求与响应模型.

**转发别名（过渡形态）**：线协议 dataclass（``PermissionContext``/
``AgentRequest``/``AgentResponse``/``AgentResponseChunk``）已随 E2A wire
契约迁入 ``gateway_protocol.e2a.agent_models``；本模块仅 re-export 同一对象，仓内既有
``from jiuwenswarm.common.schema.agent import ...`` 全部保持可用。
gateway 独立版就绪后随 common 副本收敛，别名删除。
"""

from __future__ import annotations

from gateway_protocol.e2a.agent_models import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)

__all__ = [
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
]
