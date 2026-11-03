# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentOS Router 扩展包的公开导出面（按需惰性导入）。

包级导出全部走 ``__getattr__`` 转发到各自子模块：导入
``agentos_router.agent_manager`` 等轻量子模块时不会拉起 ``extension`` →
``router_client``，避免包初始化即依赖 ``gateway.channel_manager`` 形成
"channel_manager ←→ agentos_router" 的循环导入风险，也避免只想用
models/agent_manager 的消费方被动加载路由与传输组件。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 顶层导出 → 定义它的子模块；__getattr__ 只加载对应子模块
_EXPORTS = {
    "BUILTIN_AGENT_TYPE": "agent_manager",
    "AgentCreateFailed": "agent_manager",
    "AgentCreatingTimeout": "agent_manager",
    "AgentDeleted": "agent_manager",
    "AgentManager": "agent_manager",
    "AgentRuntime": "agent_manager",
    "is_third_party_agent_type": "agent_manager",
    "normalize_agent_key_fields": "agent_manager",
    "AgentOSRouter": "extension",
    "register_extensions": "extension",
    "AgentInfo": "models",
    "AgentStatus": "models",
    "ImageInfo": "models",
    "ImageEntry": "registry_client",
    "InstanceRecord": "registry_client",
    "LaunchSpec": "registry_client",
    "RegistryClient": "registry_client",
    "RegistryConfig": "registry_client",
    "RegistryConflictError": "registry_client",
    "RegistryConnectionError": "registry_client",
    "RegistryError": "registry_client",
    "RegistryHTTPError": "registry_client",
    "RegistryNotFoundError": "registry_client",
    "RegistryValidationError": "registry_client",
    "compute_backoff_delay": "registry_client",
    "instance_service_id": "registry_client",
    "resolve_instance_kind": "registry_client",
    "AgentOSRouterClient": "router_client",
    "AgentOSThirdAgent": "third_agent",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
