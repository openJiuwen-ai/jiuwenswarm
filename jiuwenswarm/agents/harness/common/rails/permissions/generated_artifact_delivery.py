# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task-local authorization for one exact ``send_file_to_user`` call."""

from __future__ import annotations

import contextvars
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SendFileAuthorizationItem:
    """One canonical path authorized for the current send call."""

    resolved_path: Path


@dataclass(frozen=True, slots=True)
class SendFileExecutionGrant:
    """Exact path/target decision published only to the current tool task."""

    items: tuple[SendFileAuthorizationItem, ...]
    target_channels: tuple[str, ...]


@dataclass(slots=True)
class _SendFileExecutionLease:
    """One shared terminal state inherited by copied async contexts."""

    grant: SendFileExecutionGrant
    lock: threading.Lock
    terminal: bool = False


_SEND_FILE_EXECUTION_GRANT: contextvars.ContextVar[_SendFileExecutionLease | None] = (
    contextvars.ContextVar("jiuwenswarm_send_file_execution_grant", default=None)
)


def normalize_send_file_target_channels(value: Any) -> tuple[str, ...]:
    """Return the canonical delivery target set for a send invocation."""

    parsed = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = stripped
    if isinstance(parsed, str):
        candidates = (parsed,)
    elif isinstance(parsed, (list, tuple)):
        candidates = tuple(parsed)
    elif parsed is None:
        candidates = ()
    else:
        candidates = (parsed,)
    return tuple(
        sorted(
            {
                str(candidate).strip()
                for candidate in candidates
                if str(candidate).strip()
            }
        )
    )


def normalize_send_file_paths(value: Any) -> tuple[str, ...]:
    """Return the canonical ordered path tuple for a send invocation."""

    parsed = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = stripped
    if isinstance(parsed, str):
        candidates = (parsed,)
    elif isinstance(parsed, (list, tuple)):
        candidates = tuple(parsed)
    elif parsed is None:
        candidates = ()
    else:
        candidates = (parsed,)
    return tuple(
        str(candidate).strip() for candidate in candidates if str(candidate).strip()
    )


def create_send_file_execution_grant(
    requested_paths: tuple[Path | str, ...],
    *,
    target_channels: Any = None,
) -> SendFileExecutionGrant:
    """Build the smallest grant needed by the current send sink."""

    items = tuple(
        SendFileAuthorizationItem(Path(path).expanduser().resolve(strict=False))
        for path in requested_paths
    )
    if not items:
        raise ValueError("send_file_authorization_missing_path")
    return SendFileExecutionGrant(
        items=items,
        target_channels=normalize_send_file_target_channels(target_channels),
    )


def publish_send_file_execution_grant(
    grant: SendFileExecutionGrant,
) -> SendFileExecutionGrant:
    """Publish an exact grant to the current task."""

    if not isinstance(grant, SendFileExecutionGrant):
        raise TypeError("grant must be a SendFileExecutionGrant")
    clear_send_file_execution_grant()
    _SEND_FILE_EXECUTION_GRANT.set(
        _SendFileExecutionLease(grant=grant, lock=threading.Lock())
    )
    return grant


def current_send_file_execution_grant() -> SendFileExecutionGrant | None:
    """Return the unconsumed grant for the current task."""

    lease = _SEND_FILE_EXECUTION_GRANT.get()
    if lease is None:
        return None
    with lease.lock:
        return None if lease.terminal else lease.grant


def clear_send_file_execution_grant() -> None:
    """Clear a stale or consumed current-task grant."""

    lease = _SEND_FILE_EXECUTION_GRANT.get()
    _SEND_FILE_EXECUTION_GRANT.set(None)
    if lease is not None:
        with lease.lock:
            lease.terminal = True


def consume_send_file_execution_grant(
    requested_paths: tuple[Path | str, ...],
    *,
    target_channels: Any = None,
) -> tuple[SendFileAuthorizationItem, ...]:
    """Consume once, then validate the exact canonical path and target sets."""

    lease = _SEND_FILE_EXECUTION_GRANT.get()
    _SEND_FILE_EXECUTION_GRANT.set(None)
    if lease is None:
        raise ValueError("send_file_execution_grant_missing")
    with lease.lock:
        if lease.terminal:
            raise ValueError("send_file_execution_grant_missing")
        lease.terminal = True
        grant = lease.grant
    expected = create_send_file_execution_grant(
        requested_paths,
        target_channels=target_channels,
    )
    if grant != expected:
        raise ValueError("send_file_execution_grant_mismatch")
    return grant.items
