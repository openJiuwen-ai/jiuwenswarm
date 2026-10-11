# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped session id used by jiuwenswarm log formatters.

Bind on the task that emits the logs. ``asyncio.create_task`` copies the
context at creation time, so a later bind or reset on the parent does not
change an already running child. Startup logs and logs before a session id
exists stay ``<nosid>``.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

NO_SESSION_ID = "<nosid>"

log_session_id: ContextVar[str] = ContextVar("log_session_id", default="")


def set_log_session_id(session_id: str | None) -> Token[str]:
    """Bind the session id that log filters should print. Empty becomes unbound."""
    return log_session_id.set(session_id or "")


def reset_log_session_id(token: Token[str]) -> None:
    """Restore the previous log session id."""
    log_session_id.reset(token)


def current_log_session_id() -> str:
    """Return the bound session id, or ``<nosid>`` when unset/empty."""
    value = log_session_id.get()
    return value if value else NO_SESSION_ID


def bind_log_session(session_id: str | None) -> tuple[Token[str], str | None]:
    """Bind jiuwenswarm logs and, when non-empty, the openjiuwen trace id.

    The returned trace id is the previous openjiuwen value, or ``None`` when
    this call did not change it (empty session id).
    """
    sid = str(session_id or "").strip()
    token = set_log_session_id(sid)
    if not sid:
        return token, None
    from openjiuwen.core.common.logging import get_session_id, set_session_id

    previous = get_session_id() or "default_trace_id"
    set_session_id(sid)
    return token, previous


def unbind_log_session(token: Token[str], previous_trace_id: str | None) -> None:
    """Restore both log contexts captured by ``bind_log_session``."""
    reset_log_session_id(token)
    if previous_trace_id is None:
        return
    from openjiuwen.core.common.logging import set_session_id

    set_session_id(previous_trace_id)
