# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Agent 请求与响应模型.

**转发别名（过渡形态）**：线协议 dataclass（``PermissionContext``/
``AgentRequest``/``AgentResponse``/``AgentResponseChunk``）已随 E2A wire
契约迁入 ``gateway_protocol.e2a.agent_models``；本模块仅 re-export 同一对象，仓内既有
``from jiuwenswarm.common.schema.agent import ...`` 全部保持可用。
gateway 独立版就绪后随 common 副本收敛，别名删除。

``AgentRef`` 为本仓 develop 侧引入的纯数据结构（routing/ipc 同源），保留在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway_protocol.e2a.agent_models import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)


@dataclass(frozen=True)
class AgentRef:
    """后端智能体标识（纯数据结构）。

    source of truth 在本模块（与 AgentRequest.agent_ref 同源）；gateway 树
    （routing/keys）与 server 树（ipc/protocol）均从这里取用，避免
    server → gateway 的跨树依赖（check_layering R2）。
    mode: 'agent' | 'code' | 'team'
    id:   mode='agent'/'code' 时为 agent_id；mode='team' 时为 team_name
    """

    mode: str
    id: str

    @classmethod
    def default(cls) -> "AgentRef":
        return cls(mode="agent", id="default")

    def __repr__(self) -> str:
        return f"AgentRef({self.mode}:{self.id})"


__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
]
