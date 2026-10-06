# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway 模块 - 系统枢纽.

历史版本在包初始化时急切导入 ChannelManager/MessageHandler 等重量级成员,
连带整个 gateway 运行时(上千个模块)——而 ``app_web`` 这类快路径只需要
``jiuwenswarm.gateway.routing`` 下的轻量工具,却被迫先付清全部导入税,
冻结 web 服务首次可服务的耗时因此多出 ~1.4s。改用 PEP 562 惰性导出:
``from jiuwenswarm.gateway import ChannelManager`` 等既有用法完全兼容,
仅在首次属性访问时才触发真实导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 静态分析(mypy/IDE)仍然可见全部导出名
    from jiuwenswarm.gateway.channel_manager import ChannelManager
    from jiuwenswarm.gateway.health_check import (
        HEALTH_CHECK_CHANNEL_ID,
        GatewayHealthCheckService,
        HealthCheckConfig,
        IHealthCheck,
    )
    from jiuwenswarm.gateway.message_handler import MessageHandler
    from jiuwenswarm.gateway.routing.agent_client import (
        AgentServerClient,
        WebSocketAgentServerClient,
    )

__all__ = [
    "AgentServerClient",
    "WebSocketAgentServerClient",
    "ChannelManager",
    "GatewayHealthCheckService",
    "HEALTH_CHECK_CHANNEL_ID",
    "HealthCheckConfig",
    "IHealthCheck",
    "MessageHandler",
]

_LAZY_EXPORTS: dict[str, str] = {
    "AgentServerClient": "jiuwenswarm.gateway.routing.agent_client",
    "WebSocketAgentServerClient": "jiuwenswarm.gateway.routing.agent_client",
    "ChannelManager": "jiuwenswarm.gateway.channel_manager",
    "GatewayHealthCheckService": "jiuwenswarm.gateway.health_check",
    "HEALTH_CHECK_CHANNEL_ID": "jiuwenswarm.gateway.health_check",
    "HealthCheckConfig": "jiuwenswarm.gateway.health_check",
    "IHealthCheck": "jiuwenswarm.gateway.health_check",
    "MessageHandler": "jiuwenswarm.gateway.message_handler",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
