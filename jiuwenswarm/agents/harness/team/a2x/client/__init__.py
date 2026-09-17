"""A2X Registry client SDK.

Public entry points:

- :class:`A2XRegistryClient` — synchronous client
- :class:`AsyncA2XRegistryClient` — asynchronous client (mirrors ``A2XRegistryClient``)

Response dataclasses and the exception hierarchy are re-exported for
``except``/``isinstance`` use.
"""

from .async_client import AsyncA2XRegistryClient
from .client import A2XRegistryClient
from .errors import (
    A2XConnectionError,
    A2XError,
    A2XHTTPError,
    NotFoundError,
    NotOwnedError,
    ServerError,
    UnexpectedServiceTypeError,
    UserConfigDeregisterForbiddenError,  # deprecated alias
    UserConfigServiceImmutableError,
    ValidationError,
)
from .models import (
    AgentDetail,
    DatasetCreateResponse,
    DatasetDeleteResponse,
    DeregisterResponse,
    PatchResponse,
    RegisterResponse,
    Reservation,
)

__all__ = [
    "A2XRegistryClient",
    "AsyncA2XRegistryClient",
    # Errors
    "A2XError",
    "A2XConnectionError",
    "A2XHTTPError",
    "NotFoundError",
    "ValidationError",
    "UserConfigServiceImmutableError",
    "UserConfigDeregisterForbiddenError",
    "UnexpectedServiceTypeError",
    "ServerError",
    "NotOwnedError",
    # Models
    "DatasetCreateResponse",
    "DatasetDeleteResponse",
    "RegisterResponse",
    "PatchResponse",
    "DeregisterResponse",
    "AgentDetail",
    "Reservation",
]

__version__ = "0.1.5"
