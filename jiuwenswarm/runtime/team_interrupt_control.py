# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped hand-off between the Runtime lifecycle and Team cancels.

``AgentRuntime.cancel_request`` already drives the generation-aware Team stop
before it forwards the interrupt to the adapter.  The adapter's Team interrupt
path used to repeat the same cancel; by then the lifecycle had cleared the
session's live handles, so the repeated call reported "nothing to cancel" even
though the stop had succeeded.

The lifecycle binds its outcome here for the duration of the adapter call.  A
ContextVar keeps the hint request-scoped and impossible for a client to forge,
and it is reset in a ``finally`` so a later request cannot observe a stale
result.  Direct adapter callers that bypass ``AgentRuntime`` see no hint and
keep their existing behaviour.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TeamRuntimeAction:
    """Outcome of the Runtime lifecycle action already applied to a Team session."""

    intent: Literal["pause", "cancel"]
    handled: bool
    generation: int | None = None


_CURRENT_TEAM_RUNTIME_ACTION: ContextVar[TeamRuntimeAction | None] = ContextVar(
    "jiuwenswarm_team_runtime_action",
    default=None,
)


def bind_team_runtime_action(
    action: TeamRuntimeAction | None,
) -> Token[TeamRuntimeAction | None] | None:
    """Bind the lifecycle outcome for the current request.

    Args:
        action: Outcome to expose to the adapter, or ``None`` when the Runtime
            did not drive a Team lifecycle action.

    Returns:
        A reset token for :func:`reset_team_runtime_action`, or ``None`` when
        no value was bound.
    """
    if action is None:
        return None
    return _CURRENT_TEAM_RUNTIME_ACTION.set(action)


def reset_team_runtime_action(
    token: Token[TeamRuntimeAction | None] | None,
) -> None:
    """Restore the previous value; safe for the ``None`` returned when unbound."""
    if token is not None:
        _CURRENT_TEAM_RUNTIME_ACTION.reset(token)


def current_team_runtime_action() -> TeamRuntimeAction | None:
    """Return the lifecycle outcome bound by the current Runtime request, if any."""
    return _CURRENT_TEAM_RUNTIME_ACTION.get()


__all__ = [
    "TeamRuntimeAction",
    "bind_team_runtime_action",
    "current_team_runtime_action",
    "reset_team_runtime_action",
]
