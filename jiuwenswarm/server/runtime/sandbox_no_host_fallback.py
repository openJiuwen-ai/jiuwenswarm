# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task-local execution scope that forbids sandbox-to-host fallback."""

from __future__ import annotations

from contextvars import ContextVar


_NO_HOST_FALLBACK: ContextVar[bool] = ContextVar(
    "jiuwenswarm_no_host_fallback",
    default=False,
)


def no_host_fallback_required() -> bool:
    """Return whether the current sandbox operation must stay off the host."""

    return _NO_HOST_FALLBACK.get()


def require_no_host_fallback() -> None:
    """Require the next sandbox operation to fail instead of using the host."""

    _NO_HOST_FALLBACK.set(True)


def clear_no_host_fallback() -> None:
    """Clear task-local execution scope after a terminal operation."""

    _NO_HOST_FALLBACK.set(False)


__all__ = [
    "clear_no_host_fallback",
    "no_host_fallback_required",
    "require_no_host_fallback",
]
