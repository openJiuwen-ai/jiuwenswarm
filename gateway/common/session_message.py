# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared constants and helpers for Host-authenticated cross-Session messages."""

from typing import Any

SESSION_MESSAGE_INTERNAL_KEY = "_jiuwenswarm_cross_session"
SESSION_MESSAGE_ORIGIN = "cross_session_agent"
SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY = "_jiuwenswarm_session_message_owner_scope_id"
SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY = "_jiuwenswarm_session_message_anonymous_owner"

_CROSS_SESSION_HISTORY_FIELDS = (
    "message_id",
    "source_session_id",
    "source_title",
    "chain_id",
    "parent_message_id",
    "hop_count",
    "language",
)


def with_cross_session_history_metadata(
    extra: dict[str, Any] | None,
    cross_session_marker: Any,
) -> dict[str, Any] | None:
    """Persist the public origin marker on cross-Session assistant records.

    Args:
        extra: Existing history record extra fields, or None.
        cross_session_marker: The internal marker dict carried by the request
            (``params[SESSION_MESSAGE_INTERNAL_KEY]``); non-dict values leave
            ``extra`` unchanged.

    Returns:
        The extended extra dict, or None when nothing was added.
    """
    result = dict(extra or {})
    if not isinstance(cross_session_marker, dict):
        return result or None
    cross_session: dict[str, Any] = {}
    for key in _CROSS_SESSION_HISTORY_FIELDS:
        if key in cross_session_marker:
            cross_session[key] = cross_session_marker[key]
    result["message_origin"] = SESSION_MESSAGE_ORIGIN
    result["cross_session"] = cross_session
    message_id = str(cross_session.get("message_id") or "").strip()
    if message_id:
        result["session_message_id"] = message_id
    return result


__all__ = [
    "SESSION_MESSAGE_INTERNAL_KEY",
    "SESSION_MESSAGE_ORIGIN",
    "SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY",
    "SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY",
    "with_cross_session_history_metadata",
]
