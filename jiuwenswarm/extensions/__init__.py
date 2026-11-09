"""Extension public surface with transport adapters loaded lazily."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExtensionLoader": ("agent_gateway.extensions.loader", "ExtensionLoader"),
    "ExtensionManager": ("agent_gateway.extensions.manager", "ExtensionManager"),
    "ExtensionRegistry": ("agent_gateway.extensions.registry", "ExtensionRegistry"),
    "AgentServerClientExtension": (
        "agent_gateway.extensions.sdk.agent_server_client",
        "AgentServerClientExtension",
    ),
    "ApplicationPluginExtension": (
        "agent_gateway.extensions.sdk.application_plugin",
        "ApplicationPluginExtension",
    ),
    "BaseExtension": ("agent_gateway.extensions.sdk.base", "BaseExtension"),
    "CryptoUtility": (
        "agent_gateway.extensions.sdk.crypto_utility",
        "CryptoUtility",
    ),
    "ThirdAgentExtension": (
        "agent_gateway.extensions.sdk.third_agent",
        "ThirdAgentExtension",
    ),
    "ExtensionConfig": ("agent_gateway.extensions.types", "ExtensionConfig"),
    "ExtensionMetadata": ("agent_gateway.extensions.types", "ExtensionMetadata"),
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
