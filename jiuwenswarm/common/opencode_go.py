# coding: utf-8
"""OpenCode Go vendor identity and request-scoped session header helpers.

The OpenCode Go vendor is identified by ``model_client_config.vendor_key``;
it reuses the OpenAI-compatible transport, so no new ``endpoint_profile`` is
introduced (agent-core rejects unknown profiles). All injection helpers are
pure and never mutate the shared configuration dictionaries.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_gateway.common.model_selection import (
    ResolvedModel,
    ResolvedModelGroup,
    ResolvedSelection,
)

OPENCODE_GO_VENDOR_KEY = "opencode-go"
OPENCODE_GO_SESSION_HEADER = "x-opencode-session"

SCOPE_AUX = "aux"
SCOPE_PROBE = "probe:config-validate"
SCOPE_UNSCOPED = "session:unscoped"

_SESSION_ID_PREFIX = "jiuwenswarm:opencode-go:"


def is_opencode_go_client_config(mcc: dict[str, Any] | None) -> bool:
    """Return whether a model client config belongs to OpenCode Go.

    Only the persisted ``vendor_key`` is authoritative; the api_base prefix is
    deliberately ignored so that legacy manual entries are never reclassified.

    Args:
        mcc: The ``model_client_config`` mapping, or ``None``.

    Returns:
        ``True`` when ``vendor_key`` equals ``opencode-go`` (case-insensitive).
    """
    if not isinstance(mcc, dict):
        return False
    vendor_key = str(mcc.get("vendor_key") or "").strip().lower()
    return vendor_key == OPENCODE_GO_VENDOR_KEY


def derive_session_id(scope: str) -> str:
    """Derive the opaque OpenCode session identifier for a scope.

    Args:
        scope: Stable scope string, e.g. ``session:<id>``.

    Returns:
        The first 32 lowercase hex characters of the SHA-256 digest.
    """
    digest = hashlib.sha256(
        f"{_SESSION_ID_PREFIX}{scope}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _merge_session_header(
    container: dict[str, Any],
    scope: str,
    *,
    copy_headers: bool,
) -> dict[str, Any]:
    """Write the derived session header into a ``custom_headers`` container.

    Args:
        container: Mapping holding the ``custom_headers`` key.
        scope: Scope used to derive the header value.
        copy_headers: Copy the existing headers before merging.

    Returns:
        The same container for chaining convenience.
    """
    custom_headers = container.get("custom_headers")
    if isinstance(custom_headers, dict):
        custom_headers = dict(custom_headers) if copy_headers else custom_headers
    else:
        custom_headers = {}

    if not any(
        str(key).strip().lower() == OPENCODE_GO_SESSION_HEADER
        for key in custom_headers
    ):
        custom_headers[OPENCODE_GO_SESSION_HEADER] = derive_session_id(scope)

    container["custom_headers"] = custom_headers
    return container


def inject_session_header(
    mcc: dict[str, Any],
    scope: str,
) -> dict[str, Any]:
    """Inject the session header into a ``model_client_config`` dict.

    A user-provided header of any casing wins and is never duplicated.

    Args:
        mcc: The ``model_client_config`` mapping to update in place.
        scope: Scope used to derive the header value.

    Returns:
        The same mapping, updated when the vendor matches.
    """
    if not is_opencode_go_client_config(mcc):
        return mcc
    return _merge_session_header(mcc, scope, copy_headers=True)


def _inject_into_model(
    model: ResolvedModel,
    scope: str,
) -> ResolvedModel:
    """Return a copy of a resolved model with the session header injected."""
    copied = model.model_copy(deep=True)
    client_options = copied.client_options
    if not isinstance(client_options, dict):
        client_options = {}
    if not is_opencode_go_client_config(client_options):
        return copied
    _merge_session_header(client_options, scope, copy_headers=False)
    copied.client_options = client_options
    return copied


def inject_session_into_selection(
    resolved: ResolvedSelection,
    scope: str,
) -> ResolvedSelection:
    """Inject the session header into a resolved model or model group.

    Args:
        resolved: The resolved selection DTO to copy and update.
        scope: Scope used to derive the header value.

    Returns:
        A copied DTO; the input is left untouched.
    """
    if isinstance(resolved, ResolvedModelGroup):
        copied = resolved.model_copy(deep=True)
        for route in copied.routes:
            route.model = _inject_into_model(route.model, scope)
        return copied
    if not isinstance(resolved, ResolvedModel):
        # Compiler doubles and future selection shapes pass through unchanged.
        return resolved
    return _inject_into_model(resolved, scope)


def resolve_scope(session_id: str | None) -> str:
    """Build the scope string for a conversation session.

    Args:
        session_id: The conversation session id, possibly empty.

    Returns:
        ``session:<id>`` or the fixed ``session:unscoped`` fallback.
    """
    normalized = str(session_id or "").strip()
    if not normalized:
        return SCOPE_UNSCOPED
    return f"session:{normalized}"
