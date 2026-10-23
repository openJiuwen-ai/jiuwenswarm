# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.agent_models``。

本模块仅保留 ``jiuwenswarm.common.schema.agent`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.agent_models import (  # noqa: F401
    AgentRef,
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
    ReqMethod,
)

__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
    "ReqMethod",
]
