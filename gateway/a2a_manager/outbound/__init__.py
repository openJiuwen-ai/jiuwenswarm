"""A2A outbound domain models and persistence contracts.

Public names are imported lazily so ingress code can depend on this package
before the outbound implementation modules are present.
"""

from importlib import import_module

_EXPORTS = {
    "A2AOutboundCredentialStore": ".credentials",
    "A2AOutboundDiscoveryService": ".discovery",
    "DiscoveredCard": ".discovery",
    "A2AOutboundDispatcher": ".dispatcher",
    "EnterpriseA2AAgentView": ".enterprise",
    "EnterpriseA2AProjection": ".enterprise",
    "A2AOutboundError": ".errors",
    "A2AOutboundErrorCode": ".errors",
    "safe_error_summary": ".errors",
    "A2ACompatibleInterface": ".models",
    "A2ADiscoveredAgent": ".models",
    "A2AOutboundAgent": ".models",
    "A2AOutboundAvailability": ".models",
    "A2AOutboundDiscovery": ".models",
    "A2AOutboundDispatch": ".models",
    "A2AOutboundDispatchMode": ".models",
    "A2AOutboundDispatchStatus": ".models",
    "TERMINAL_DISPATCH_STATUSES": ".models",
    "A2A_OUTBOUND_AGENT_STORE_NAME": ".repository",
    "A2A_OUTBOUND_DISPATCH_STORE_NAME": ".repository",
    "A2AOutboundRepository": ".repository",
    "JsonA2AOutboundRecordCodec": ".repository",
    "A2AOutboundRegistry": ".registry",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = list(_EXPORTS)
