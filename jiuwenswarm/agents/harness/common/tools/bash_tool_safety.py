# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply jiuwenswarm shell safety rules to openjiuwen BashTool / PowerShellTool.

The agent's primary shell tool is ``bash`` (openjiuwen ``BashTool``), not
``mcp_exec_command``.  Safety checks in ``command_tools`` only affect the latter
unless we hook the harness tools here.
"""

from __future__ import annotations

import contextlib
import os
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


def _kill_windows_descendants(pid: int | None) -> None:
    """Kill every live descendant of *pid*, deepest first."""
    if pid is None:
        return
    import psutil

    try:
        descendants = psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return
    for proc in reversed(descendants):
        with contextlib.suppress(psutil.Error):
            proc.kill()


def _install_windows_tree_kill() -> None:
    """Make shell-tool timeouts and interrupts kill the whole tree on Windows.

    openjiuwen kills only the direct child there (cmd / powershell / bash), so
    everything a command started (python -> node -> chrome ...) kept running
    after each timeout or interrupt. POSIX already kills the process group.
    Processes whose parent has already exited cannot be found this way.

    Deliberately patches openjiuwen private internals (hence the waiver
    below) until the tree-kill moves into openjiuwen itself as one shared
    helper; every access is guarded by the jiuwenswarm_tree_kill marker so
    re-installation never double-wraps.
    """
    # pylint: disable=protected-access
    from openjiuwen.core.sys_operation import shell_process_registry
    from openjiuwen.core.sys_operation.local.utils import AsyncProcessHandler

    from jiuwenswarm.agents.harness.common.tools import command_tools

    original_kill_tree = AsyncProcessHandler._kill_process_tree
    if not getattr(original_kill_tree, "jiuwenswarm_tree_kill", False):

        def _kill_process_tree(self: Any) -> None:
            # An exited-and-reaped process may have a recycled PID: its
            # "descendants" would then belong to a stranger. asyncio fills
            # returncode once it observes the exit, and until then the PID
            # cannot be recycled, so a None check is a safe liveness gate.
            if getattr(self._process, "returncode", None) is None:
                _kill_windows_descendants(self._process.pid)
            original_kill_tree(self)

        _kill_process_tree.jiuwenswarm_tree_kill = True
        AsyncProcessHandler._kill_process_tree = _kill_process_tree

    original_terminate = shell_process_registry.terminate_shell_process
    if not getattr(original_terminate, "jiuwenswarm_tree_kill", False):

        def terminate_shell_process(proc: Any) -> bool:
            # Popen-style handles keep returncode None until somebody polls,
            # so an already-exited child would slip through a bare attribute
            # check and its recycled PID's "descendants" would be killed.
            if getattr(proc, "returncode", None) is None:
                poll = getattr(proc, "poll", None)
                if poll is not None:
                    with contextlib.suppress(Exception):
                        poll()
            if getattr(proc, "returncode", None) is None:
                _kill_windows_descendants(getattr(proc, "pid", None))
            return original_terminate(proc)

        terminate_shell_process.jiuwenswarm_tree_kill = True
        shell_process_registry.terminate_shell_process = terminate_shell_process
        # command_tools imported the function by name, so rebind it there too.
        command_tools.terminate_shell_process = terminate_shell_process


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

    if os.name == "nt":
        _install_windows_tree_kill()

    _installed = True


def reset_installed_flag() -> None:
    """Reset the installed flag so hooks can be re-applied (for testing)."""
    global _installed
    _installed = False


__all__ = ["install_shell_tool_safety_hooks", "reset_installed_flag"]
