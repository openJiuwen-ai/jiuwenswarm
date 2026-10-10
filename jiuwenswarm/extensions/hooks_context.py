# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：Hook 上下文 dataclass 已迁 ``gateway_protocol.hooks``。

MemoryHookContext / GatewayChatHookContext / AgentServerChatHookContext /
SystemPromptHookContext 契约以 protocol 包为 source of truth；本文件属扩展框架
通用模块（随 gateway 仓迁移整体退役），当前保留旧 import 路径供消费方过渡。
"""

from __future__ import annotations

from gateway_protocol.hooks import (  # noqa: F401
    AgentServerChatHookContext,
    GatewayChatHookContext,
    MemoryHookContext,
    SystemPromptHookContext,
)

__all__ = [
    "AgentServerChatHookContext",
    "GatewayChatHookContext",
    "MemoryHookContext",
    "SystemPromptHookContext",
]
