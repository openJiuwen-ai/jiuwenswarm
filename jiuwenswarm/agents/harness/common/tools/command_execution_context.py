"""Request-local command execution ownership."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandExecutionBinding:
    """Exact sys operation selected by the owning session adapter."""

    sys_operation: Any
    sandboxed: bool

    def __post_init__(self) -> None:
        if self.sys_operation is None:
            raise ValueError("command execution sys_operation is required")
        if not isinstance(self.sandboxed, bool):
            raise TypeError("command execution sandboxed flag must be bool")


_COMMAND_EXECUTION_BINDING: ContextVar[CommandExecutionBinding | None] = ContextVar(
    "jiuwenswarm_command_execution_binding",
    default=None,
)


def bind_command_execution(
    sys_operation: Any,
    *,
    sandboxed: bool,
) -> Token[CommandExecutionBinding | None]:
    """Bind one adapter-owned sys operation to the current task tree."""

    return _COMMAND_EXECUTION_BINDING.set(
        CommandExecutionBinding(
            sys_operation=sys_operation,
            sandboxed=sandboxed,
        )
    )


def bind_no_command_execution() -> Token[CommandExecutionBinding | None]:
    """Explicitly shadow an inherited command binding for this task."""

    return _COMMAND_EXECUTION_BINDING.set(None)


def current_command_execution() -> CommandExecutionBinding | None:
    """Return the current request-local command execution binding."""

    return _COMMAND_EXECUTION_BINDING.get()


def reset_command_execution(token: Token[CommandExecutionBinding | None]) -> None:
    """Restore the binding that preceded the owning request boundary."""

    _COMMAND_EXECUTION_BINDING.reset(token)
