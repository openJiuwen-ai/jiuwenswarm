"""A2A outbound domain models and persistence contracts."""

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
