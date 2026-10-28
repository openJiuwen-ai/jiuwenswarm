"""Extension public surface with transport adapters loaded lazily."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExtensionLoader": ("gateway.extensions.loader", "ExtensionLoader"),
    "ExtensionManager": ("gateway.extensions.manager", "ExtensionManager"),
    "ExtensionRegistry": ("gateway.extensions.registry", "ExtensionRegistry"),
    "AgentServerClientExtension": (
        "gateway.extensions.sdk.agent_server_client",
        "AgentServerClientExtension",
    ),
    "ApplicationPluginExtension": (
        "gateway.extensions.sdk.application_plugin",
        "ApplicationPluginExtension",
    ),
    "BaseExtension": ("gateway.extensions.sdk.base", "BaseExtension"),
    "CryptoUtility": (
        "gateway.extensions.sdk.crypto_utility",
        "CryptoUtility",
    ),
    "ThirdAgentExtension": (
        "gateway.extensions.sdk.third_agent",
        "ThirdAgentExtension",
    ),
    "ExtensionConfig": ("gateway.extensions.types", "ExtensionConfig"),
    "ExtensionMetadata": ("gateway.extensions.types", "ExtensionMetadata"),
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
