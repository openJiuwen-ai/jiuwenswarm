# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped skill-root binding for disk-only evolution RPCs."""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path
from typing import Sequence

_SESSION_REGISTERED_SKILL_DIRS: ContextVar[tuple[str, ...] | None] = ContextVar(
    "jiuwenswarm_session_registered_skill_dirs",
    default=None,
)


def bind_session_registered_skill_dirs(dirs: Sequence[str | Path]) -> Token:
    """Bind skill roots for the current task/request; return reset token."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in dirs:
        try:
            key = str(Path(str(raw)).expanduser().resolve())
        except OSError:
            key = str(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return _SESSION_REGISTERED_SKILL_DIRS.set(tuple(normalized))


def reset_session_registered_skill_dirs(token: Token) -> None:
    """Reset skill-root binding to the previous value."""
    _SESSION_REGISTERED_SKILL_DIRS.reset(token)


def get_session_registered_skill_dirs() -> list[str] | None:
    """Return request-bound skill roots, or ``None`` when unbound."""
    bound = _SESSION_REGISTERED_SKILL_DIRS.get()
    if bound is None:
        return None
    return list(bound)


__all__ = [
    "bind_session_registered_skill_dirs",
    "get_session_registered_skill_dirs",
    "reset_session_registered_skill_dirs",
]
