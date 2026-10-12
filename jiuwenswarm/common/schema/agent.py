# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Agent 请求与响应模型.

**转发别名（过渡形态）**：线协议 dataclass（``PermissionContext``/
``AgentRequest``/``AgentResponse``/``AgentResponseChunk``）已随 E2A wire
契约迁入 ``gateway_protocol.e2a.agent_models``；本模块仅 re-export 同一对象，仓内既有
``from jiuwenswarm.common.schema.agent import ...`` 全部保持可用。
gateway 独立版就绪后随 common 副本收敛，别名删除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gateway_protocol.e2a.agent_models import (
    AgentRequest as _GatewayAgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)


@dataclass
class AgentRequest(_GatewayAgentRequest):
    """Agent 请求（Gateway → AgentServer），含 AgentServer-only 运行时绑定."""

    # AgentServer-only runtime dispatch result. The facade resolves it before
    # MCP reconciliation so the root adapter and session child consume the
    # same manifest-derived binding without resolving the package twice.
    trusted_runtime_binding: dict[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


__all__ = [
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
]
