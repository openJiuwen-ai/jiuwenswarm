"""Extension SDK exports; transport-specific contracts are lazy imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_APPLICATION_PLUGIN_MODULE = "jiuwenswarm.extensions.sdk.application_plugin"

_EXPORTS = {
    "AgentServerClientExtension": (
        "jiuwenswarm.extensions.sdk.agent_server_client",
        "AgentServerClientExtension",
    ),
    "ApplicationPluginExtension": (
        _APPLICATION_PLUGIN_MODULE,
        "ApplicationPluginExtension",
    ),
    "ApplicationPluginServices": (
        _APPLICATION_PLUGIN_MODULE,
        "ApplicationPluginServices",
    ),
    "FrontendContribution": (
        _APPLICATION_PLUGIN_MODULE,
        "FrontendContribution",
    ),
    "ManifestApplicationPlugin": (
        _APPLICATION_PLUGIN_MODULE,
        "ManifestApplicationPlugin",
    ),
    "WebSocketRouteContribution": (
        _APPLICATION_PLUGIN_MODULE,
        "WebSocketRouteContribution",
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
