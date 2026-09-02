# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply jiuwenswarm shell safety rules to openjiuwen BashTool / PowerShellTool.

The agent's primary shell tool is ``bash`` (openjiuwen ``BashTool``), not
``mcp_exec_command``.  Safety checks in ``command_tools`` only affect the latter
unless we hook the harness tools here.
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
        _check_command_safety,
        _check_worktree_path_safety,
        _enforce_tui_spawn_budget,
    )

    blocked = _check_command_safety(command)
    if blocked:
        return f"[ERROR]: command rejected for safety ({blocked})."
    worktree_block = _check_worktree_path_safety(command)
    if worktree_block:
        return f"[ERROR]: {worktree_block}"
    spawn_block = _enforce_tui_spawn_budget(command, resolve_shell_session_id() or "")
    if spawn_block:
        return f"[ERROR]: {spawn_block}"
    return None


def _shell_mismatch(tool_name: str, command: str) -> str | None:
    """Reject commands whose syntax belongs to a different shell tool."""
    if tool_name == "bash":
        from jiuwenswarm.agents.harness.common.tools.command_tools import _is_powershell_command

        if _is_powershell_command(command):
            return (
                "PowerShell syntax was sent to the bash tool; retry with the "
                "powershell tool (or mcp_exec_command with shell_type=\"powershell\")."
            )
    return None


def _wrap_invoke(
    original: Callable[..., Awaitable[Any]],
    tool_name: str,
) -> Callable[..., Awaitable[Any]]:
    from openjiuwen.harness.tools.base_tool import ToolOutput

    async def invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        parsed = getattr(self, "_parse_inputs")(inputs)
        if parsed.command:
            mismatch = _shell_mismatch(tool_name, parsed.command)
            if mismatch:
                return ToolOutput(success=False, error=mismatch)
            err = _pre_execute_shell_command(parsed.command)
            if err:
                return ToolOutput(success=False, error=err)
        routed_inputs = dict(inputs)
        routed_inputs["shell_type"] = tool_name
        return await original(self, routed_inputs, **kwargs)

    invoke.jiuwenswarm_safety_wrapped = True
    return invoke


def _wrap_stream(
    original: Callable[..., Any],
    tool_name: str,
) -> Callable[..., Any]:
    from openjiuwen.harness.tools.base_tool import ToolOutput

    async def stream(self: Any, inputs: dict[str, Any], **kwargs: Any):
        parsed = getattr(self, "_parse_inputs")(inputs)
        if parsed.command:
            mismatch = _shell_mismatch(tool_name, parsed.command)
            if mismatch:
                yield ToolOutput(success=False, error=mismatch)
                return
            err = _pre_execute_shell_command(parsed.command)
            if err:
                yield ToolOutput(success=False, error=err)
                return
        routed_inputs = dict(inputs)
        routed_inputs["shell_type"] = tool_name
        async for item in original(self, routed_inputs, **kwargs):
            yield item

    stream.jiuwenswarm_safety_wrapped = True
    return stream


def _patch_tool_class(tool_cls: type, tool_name: str) -> None:
    if not getattr(tool_cls.invoke, "jiuwenswarm_safety_wrapped", False):
        tool_cls.invoke = _wrap_invoke(tool_cls.invoke, tool_name)
    if not getattr(tool_cls.stream, "jiuwenswarm_safety_wrapped", False):
        tool_cls.stream = _wrap_stream(tool_cls.stream, tool_name)


def _contains_unquoted_semicolon(command: str) -> bool:
    quote: str | None = None
    for char in command:
        if char in {'"', "'"}:
            quote = None if quote == char else char if quote is None else quote
        elif char == ";" and quote is None:
            return True
    return False


def _patch_shell_execution_plan() -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module

    operation_cls = shell_module.ShellOperation
    original = operation_cls._resolve_execution_plan
    if getattr(original, "jiuwenswarm_semicolon_routing_wrapped", False):
        return

    def resolve_execution_plan(command: str, shell_type: Any) -> tuple[list[str] | str, bool, str]:
        plan, use_shell, resolved_shell = original(command, shell_type)
        if resolved_shell != "cmd" or getattr(shell_type, "value", shell_type) != "auto":
            return plan, use_shell, resolved_shell

        if not _contains_unquoted_semicolon(command):
            return plan, use_shell, resolved_shell

        exe = shell_module._available_bash(allow_wsl=False)
        if exe:
            normalized = shell_module._normalize_windows_paths_for_bash(command)
            return [exe, "-lc", normalized], False, "bash"

        ps_exe = shell_module._available_powershell()
        return [ps_exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"

    resolve_execution_plan.jiuwenswarm_semicolon_routing_wrapped = True
    operation_cls._resolve_execution_plan = staticmethod(resolve_execution_plan)


def install_shell_tool_safety_hooks() -> None:
    """Idempotently wire safety checks into harness shell tools."""
    global _installed
    if _installed:
        return

    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    _patch_tool_class(BashTool, "bash")
    _patch_shell_execution_plan()

    try:
        from openjiuwen.harness.tools.shell.powershell._tool import PowerShellTool

        _patch_tool_class(PowerShellTool, "powershell")
    except ImportError:
        pass

    _installed = True


def reset_installed_flag() -> None:
    """Reset the installed flag so hooks can be re-applied (for testing)."""
    global _installed
    _installed = False


__all__ = [
    "_pre_execute_shell_command",
    "_shell_mismatch",
    "_wrap_invoke",
    "install_shell_tool_safety_hooks",
    "reset_installed_flag",
]
