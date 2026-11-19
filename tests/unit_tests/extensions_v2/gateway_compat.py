"""Defer only explicit capabilities absent from the pinned external Gateway."""

from importlib import import_module
from types import ModuleType

import pytest

_DEFERRED_MODULES = frozenset({
    "agent_gateway.channel_manager.web.extension_package_handlers",
    "agent_gateway.channel_manager.web.webui_handlers",
    "agent_gateway.channel_manager.web.webui_resource_http",
    "agent_gateway.extensions_v2.management.catalog",
})


def require_gateway_module(name: str) -> ModuleType:
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        missing = error.name or ""
        if (name in _DEFERRED_MODULES and missing.startswith("agent_gateway.")
                and (name == missing or name.startswith(missing + "."))):
            pytest.skip(f"Pinned agent-gateway e95c5c2 does not provide {name}")
        raise
