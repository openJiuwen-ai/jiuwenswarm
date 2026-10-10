"""Extension SDK exports; contract modules resolve to gateway_protocol.

惰性机制保留：transport 相关契约仍按需 import；纯契约模块已迁
``gateway_protocol.{types,sdk}``，此处的旧路径仅为过渡期兼容
（与 common/e2a 转发别名同模式，同一对象非副本）。
ExtensionLoader/Manager/Registry 为实现类，仍由本仓扩展框架提供。
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
    "ApplicationPluginServices": (
        "gateway_protocol.sdk.application_plugin",
        "ApplicationPluginServices",
    ),
    "FrontendContribution": (
        "gateway_protocol.sdk.application_plugin",
        "FrontendContribution",
    ),
    "ManifestApplicationPlugin": (
        "gateway_protocol.sdk.application_plugin",
        "ManifestApplicationPlugin",
    ),
    "WebSocketRouteContribution": (
        "gateway_protocol.sdk.application_plugin",
        "WebSocketRouteContribution",
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
