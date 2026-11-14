# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply the TUI spawn budget to openjiuwen BashTool / PowerShellTool.

Permission adjudication (host guard, command allowlist, file guard, ...) lives
in ``PermissionInterruptRail`` → ``PermissionManager.check_permission``. This
hook only keeps the JiuwenSwarm-specific TUI spawn rate limit.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

_installed = False


def _pre_execute_shell_command(command: str) -> str | None:
    """Return an error string when *command* must not run; else None."""
    from openjiuwen.core.sys_operation.shell_process_registry import (
        resolve_shell_session_id,
    )

    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _enforce_tui_spawn_budget,
    )

    spawn_block = _enforce_tui_spawn_budget(command, resolve_shell_session_id() or "")
    if spawn_block:
        return f"[ERROR]: {spawn_block}"
    return None


def _wrap_invoke(
    original: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    from openjiuwen.harness.tools.base_tool import ToolOutput

    async def invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        parsed = getattr(self, "_parse_inputs")(inputs)
        if parsed.command:
            err = _pre_execute_shell_command(parsed.command)
            if err:
                return ToolOutput(success=False, error=err)
        return await original(self, inputs, **kwargs)

    invoke.jiuwenswarm_safety_wrapped = True
    return invoke


def _wrap_stream(
    original: Callable[..., Any],
) -> Callable[..., Any]:
    from openjiuwen.harness.tools.base_tool import ToolOutput

    async def stream(self: Any, inputs: dict[str, Any], **kwargs: Any):
        parsed = getattr(self, "_parse_inputs")(inputs)
        if parsed.command:
            err = _pre_execute_shell_command(parsed.command)
            if err:
                yield ToolOutput(success=False, error=err)
                return
        async for item in original(self, inputs, **kwargs):
            yield item

    stream.jiuwenswarm_safety_wrapped = True
    return stream


def _patch_tool_class(tool_cls: type) -> None:
    if not getattr(tool_cls.invoke, "jiuwenswarm_safety_wrapped", False):
        tool_cls.invoke = _wrap_invoke(tool_cls.invoke)
    if not getattr(tool_cls.stream, "jiuwenswarm_safety_wrapped", False):
        tool_cls.stream = _wrap_stream(tool_cls.stream)


def install_shell_tool_safety_hooks() -> None:
    """Idempotently wire safety checks into harness shell tools."""
    global _installed
    if _installed:
        return

    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    _patch_tool_class(BashTool)

    try:
        from openjiuwen.harness.tools.shell.powershell._tool import PowerShellTool

        _patch_tool_class(PowerShellTool)
    except ImportError:
        pass

    _installed = True


def reset_installed_flag() -> None:
    """Reset the installed flag so hooks can be re-applied (for testing)."""
    global _installed
    _installed = False


__all__ = ["install_shell_tool_safety_hooks", "reset_installed_flag"]
