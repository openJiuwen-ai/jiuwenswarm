# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 接入适配模块。

实现 AgentSSASBackendProtocol,接收事件并返回风险评估。
0.1 版本支持进程内模式(AgentSSASBackend)。
远程 HTTP 模式(AgentSSASRemoteBackend)为后续版本拓展。
"""

from agent_ssas.core.framework.access_adapter.adapter import AccessAdapter
from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.access_adapter.protocol import AgentSSASBackendProtocol

__all__ = [
    "AccessAdapter",
    "AgentSSASBackend",
    "AgentSSASBackendProtocol",
]
