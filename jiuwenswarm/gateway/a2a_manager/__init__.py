"""A2A ingress lifecycle, configuration persistence, and management API types."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import (
        A2AIngressConfigRepository,
        A2AOutboundSettingsRepository,
        load_a2a_ingress_config,
        load_a2a_ingress_config_safely,
    )
    from .manager import A2AManager
    from .models import A2AIngressConfig, A2AIngressError, A2AIngressSnapshot, A2AIngressState

# AgentServer imports tool_rpc and outbound errors through this package. Keep
# Gateway service implementations unloaded until a caller requests their exports.
_EXPORTS = {
    "A2AIngressConfig": ".models",
    "A2AIngressConfigRepository": ".config",
    "A2AIngressError": ".models",
    "A2AIngressSnapshot": ".models",
    "A2AIngressState": ".models",
    "A2AManager": ".manager",
    "A2AOutboundSettingsRepository": ".config",
    "load_a2a_ingress_config": ".config",
    "load_a2a_ingress_config_safely": ".config",
}

__all__ = [
    "A2AIngressConfig", "A2AIngressConfigRepository", "A2AIngressError", "A2AIngressSnapshot",
    "A2AIngressState", "A2AManager", "A2AOutboundSettingsRepository",
    "load_a2a_ingress_config", "load_a2a_ingress_config_safely",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
