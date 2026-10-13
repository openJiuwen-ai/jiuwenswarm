"""Extension public surface with transport adapters loaded lazily.

SDK 基类与接口为本仓实现副本（gateway_protocol 仓收敛为纯数据结构后，
接口/基类回各仓持有；两仓经组装期注入对齐）；ExtensionConfig/ExtensionMetadata
为纯数据结构，仍以 ``gateway_protocol.types`` 为 source of truth；
ExtensionLoader/Manager/Registry 为具体实现，仍由本仓扩展框架提供。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExtensionLoader": ("jiuwenswarm.extensions.loader", "ExtensionLoader"),
    "ExtensionManager": ("jiuwenswarm.extensions.manager", "ExtensionManager"),
    "ExtensionRegistry": ("jiuwenswarm.extensions.registry", "ExtensionRegistry"),
    "AgentServerClientExtension": (
        "jiuwenswarm.extensions.sdk.agent_server_client",
        "AgentServerClientExtension",
    ),
    "ApplicationPluginExtension": (
        "jiuwenswarm.extensions.sdk.application_plugin",
        "ApplicationPluginExtension",
    ),
    "BaseExtension": ("jiuwenswarm.extensions.sdk.base", "BaseExtension"),
    "CryptoUtility": (
        "jiuwenswarm.extensions.sdk.crypto_utility",
        "CryptoUtility",
    ),
    "ThirdAgentExtension": (
        "jiuwenswarm.extensions.sdk.third_agent",
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
