"""Exception hierarchy for the A2X Registry client SDK.

All errors raised by the SDK inherit from ``A2XError``. HTTP-origin errors
further inherit from ``A2XHTTPError`` and carry ``status_code`` / ``payload``;
local-only errors (e.g. ownership violations) do not.
"""

from __future__ import annotations

from typing import Any


class A2XError(Exception):
    """Base class for all SDK errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class A2XConnectionError(A2XError):
    """Network / timeout failures (httpx.ConnectError, TimeoutException, ...)."""


class A2XHTTPError(A2XError):
    """Any 4xx/5xx response from the backend."""


class NotFoundError(A2XHTTPError):
    """404 — resource not found."""


class ValidationError(A2XHTTPError):
    """400 / 422 — request rejected by the backend."""


class UserConfigServiceImmutableError(ValidationError):
    """Update or delete refused because the service originates from ``user_config``.

    The backend rejects both ``PUT`` and ``DELETE`` on services whose ``source``
    is ``user_config`` — callers must edit ``user_config.json`` directly.
    """


# Backward-compatible alias (old name was deregister-specific, but the same
# backend rejection covers update_agent too). Will be removed in a future major.
UserConfigDeregisterForbiddenError = UserConfigServiceImmutableError


class UnexpectedServiceTypeError(A2XHTTPError):
    """``get_agent`` received a non-JSON payload (e.g. a skill ZIP)."""


class ServerError(A2XHTTPError):
    """5xx — backend internal error."""


class NotOwnedError(A2XError):
    """Local ownership check failed; no HTTP request was sent."""

    def __init__(self, dataset: str, service_id: str) -> None:
        super().__init__(
            f"service {service_id!r} in dataset {dataset!r} was not registered by this client"
        )
        self.dataset = dataset
        self.service_id = service_id
