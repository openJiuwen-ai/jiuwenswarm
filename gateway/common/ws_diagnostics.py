# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for WebSocket diagnostic logging."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _close_frame_info(frame: Any) -> dict[str, Any] | None:
    if frame is None:
        return None
    return {
        "code": _normalize_value(getattr(frame, "code", None)),
        "reason": getattr(frame, "reason", None),
    }


def describe_ws_exception(exc: BaseException) -> dict[str, Any]:
    """Return stable, version-tolerant fields for WebSocket-related exceptions."""
    rcvd = getattr(exc, "rcvd", None)
    sent = getattr(exc, "sent", None)
    close_code = getattr(exc, "code", None)
    close_reason = getattr(exc, "reason", None)

    if close_code is None:
        rcvd_code = getattr(rcvd, "code", None)
        sent_code = getattr(sent, "code", None)
        close_code = rcvd_code if rcvd_code is not None else sent_code
    if close_reason is None:
        rcvd_reason = getattr(rcvd, "reason", None)
        sent_reason = getattr(sent, "reason", None)
        close_reason = rcvd_reason if rcvd_reason is not None else sent_reason

    return {
        "exc_type": type(exc).__name__,
        "message": str(exc),
        "repr": repr(exc),
        "close_code": _normalize_value(close_code),
        "close_reason": close_reason,
        "rcvd": _close_frame_info(rcvd),
        "sent": _close_frame_info(sent),
        "rcvd_then_sent": getattr(exc, "rcvd_then_sent", None),
    }


def describe_ws_peer(ws: Any) -> dict[str, Any]:
    """Return best-effort connection fields without depending on a concrete ws class."""
    if ws is None:
        return {
            "ws_id": None,
            "remote": None,
            "local": None,
            "ws_closed": None,
            "ws_state": None,
        }
    return {
        "ws_id": id(ws),
        "remote": getattr(ws, "remote_address", None),
        "local": getattr(ws, "local_address", None),
        "ws_closed": getattr(ws, "closed", None),
        "ws_state": getattr(ws, "state", None),
    }


def format_ws_diagnostics(*parts: Mapping[str, Any] | None, **fields: Any) -> str:
    """Format diagnostic fields as stable ``key=value`` pairs for logs."""
    merged: dict[str, Any] = {}
    for part in parts:
        if part:
            merged.update(part)
    merged.update(fields)
    return " ".join(f"{key}={_normalize_value(value)!r}" for key, value in merged.items())
