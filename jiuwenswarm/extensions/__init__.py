"""Extension public surface with transport adapters loaded lazily.

纯契约符号（SDK 基类/数据类型）已迁 ``gateway_protocol.{sdk,types}``（支线
计划 E 后 source of truth，转发别名同一对象）；ExtensionLoader/Manager/Registry
为具体实现，仍由本仓扩展框架提供、待 gateway 仓迁移。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExtensionLoader": ("jiuwenswarm.extensions.loader", "ExtensionLoader"),
    "ExtensionManager": ("jiuwenswarm.extensions.manager", "ExtensionManager"),
    "ExtensionRegistry": ("jiuwenswarm.extensions.registry", "ExtensionRegistry"),
    "AgentServerClientExtension": (
        "gateway_protocol.sdk.agent_server_client",
        "AgentServerClientExtension",
    ),
    "ApplicationPluginExtension": (
        "gateway_protocol.sdk.application_plugin",
        "ApplicationPluginExtension",
    ),
    "BaseExtension": ("gateway_protocol.sdk.base", "BaseExtension"),
    "CryptoUtility": (
        "gateway_protocol.sdk.crypto_utility",
        "CryptoUtility",
    ),
    "ThirdAgentExtension": (
        "gateway_protocol.sdk.third_agent",
        "ThirdAgentExtension",
    ),
    "ExtensionConfig": ("gateway_protocol.types", "ExtensionConfig"),
    "ExtensionMetadata": ("gateway_protocol.types", "ExtensionMetadata"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
