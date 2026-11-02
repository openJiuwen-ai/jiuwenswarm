"""A2A outbound domain models and persistence contracts."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .credentials import A2AOutboundCredentialStore
    from .discovery import A2AOutboundDiscoveryService, DiscoveredCard
    from .dispatcher import A2AOutboundDispatcher
    from .enterprise import EnterpriseA2AAgentView, EnterpriseA2AProjection
    from .errors import A2AOutboundError, A2AOutboundErrorCode, safe_error_summary
    from .models import (
        A2ACompatibleInterface,
        A2ADiscoveredAgent,
        A2AOutboundAgent,
        A2AOutboundAvailability,
        A2AOutboundDiscovery,
        A2AOutboundDispatch,
        A2AOutboundDispatchMode,
        A2AOutboundDispatchStatus,
        TERMINAL_DISPATCH_STATUSES,
    )
    from .repository import (
        A2A_OUTBOUND_AGENT_STORE_NAME,
        A2A_OUTBOUND_DISPATCH_STORE_NAME,
        A2AOutboundRepository,
        JsonA2AOutboundRecordCodec,
    )
    from .registry import A2AOutboundRegistry

# Error handling and tool metadata must not eagerly load discovery/dispatch SDKs.
# Preserve the package API while resolving each implementation on first access.
_EXPORTS = {
    "A2ACompatibleInterface": ".models",
    "A2ADiscoveredAgent": ".models",
    "A2AOutboundAgent": ".models",
    "A2AOutboundAvailability": ".models",
    "A2AOutboundCredentialStore": ".credentials",
    "A2AOutboundDiscoveryService": ".discovery",
    "A2AOutboundDispatcher": ".dispatcher",
    "EnterpriseA2AAgentView": ".enterprise",
    "EnterpriseA2AProjection": ".enterprise",
    "A2AOutboundRegistry": ".registry",
    "A2AOutboundDiscovery": ".models",
    "A2AOutboundDispatch": ".models",
    "A2AOutboundDispatchMode": ".models",
    "A2AOutboundDispatchStatus": ".models",
    "A2AOutboundError": ".errors",
    "A2AOutboundErrorCode": ".errors",
    "DiscoveredCard": ".discovery",
    "A2AOutboundRepository": ".repository",
    "A2A_OUTBOUND_AGENT_STORE_NAME": ".repository",
    "A2A_OUTBOUND_DISPATCH_STORE_NAME": ".repository",
    "JsonA2AOutboundRecordCodec": ".repository",
    "TERMINAL_DISPATCH_STATUSES": ".models",
    "safe_error_summary": ".errors",
}

__all__ = [
    "A2ACompatibleInterface",
    "A2ADiscoveredAgent",
    "A2AOutboundAgent",
    "A2AOutboundAvailability",
    "A2AOutboundCredentialStore",
    "A2AOutboundDiscoveryService",
    "A2AOutboundDispatcher",
    "EnterpriseA2AAgentView",
    "EnterpriseA2AProjection",
    "A2AOutboundRegistry",
    "A2AOutboundDiscovery",
    "A2AOutboundDispatch",
    "A2AOutboundDispatchMode",
    "A2AOutboundDispatchStatus",
    "A2AOutboundError",
    "A2AOutboundErrorCode",
    "DiscoveredCard",
    "A2AOutboundRepository",
    "A2A_OUTBOUND_AGENT_STORE_NAME",
    "A2A_OUTBOUND_DISPATCH_STORE_NAME",
    "JsonA2AOutboundRecordCodec",
    "TERMINAL_DISPATCH_STATUSES",
    "safe_error_summary",
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
