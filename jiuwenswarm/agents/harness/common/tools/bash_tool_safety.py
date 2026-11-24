# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply the TUI spawn budget to openjiuwen BashTool / PowerShellTool.

Permission adjudication (host guard, command allowlist, file guard, ...) lives
in ``PermissionInterruptRail`` → ``PermissionManager.check_permission``. This
hook only keeps the JiuwenSwarm-specific TUI spawn rate limit.
"""

from __future__ import annotations

import functools
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


def _stamp_exempt_tool_card(tool_cls: type) -> None:
    """Exempt shell tools from the 300s default outer tool-call timeout.

    ``bash`` / ``powershell`` take their time budget per call (``timeout``
    seconds, default 300, up to 3600). The framework also wraps every
    ``tool.invoke`` in ``AbilityManager``'s ``DEFAULT_TOOL_CALL_TIMEOUT`` (300s)
    ``anyio.fail_after``. Because their ToolCards declared no
    ``resilience.timeout_s``, that 300s cap silently overrode the caller: a
    command with ``timeout=600`` was killed at 300s, and the log evidence is
    literal - ``Tool 'bash' timed out after 300.0s`` right after the model
    passed ``timeout: 900``.

    Declaring ``timeout_s = None`` means "do not apply the 300s default"; the
    call still gets a ceiling from one of two places:

    - agent-core's ``_resolve_call_timeout_for_args`` derives the deadline from
      this call's own ``timeout`` argument (``min(T, 3600) + settle grace``), so
      the inner timeout fires first and the caller keeps the partial output;
    - on a runtime that predates that resolver, the call site falls back to
      ``MAX_TOOL_CALL_TIMEOUT_HARD_LIMIT`` (3600s).

    Same shape as ``task_tool`` / ``skill_turbo`` / ``deepresearch_execute``.

    Stamped on the class rather than at each construction site because shell
    tools are built in several rails (deep ``ConcurrentSafeSysOperationRail``,
    flash ``SlimSysOperationRail``, ``FlashBashSysOperationRail``); patching
    ``__init__`` covers every one of them.
    """
    original_init = tool_cls.__init__
    if getattr(original_init, "jiuwenswarm_card_exempt", False):
        return

    @functools.wraps(original_init)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        card = getattr(self, "card", None)
        properties = getattr(card, "properties", None)
        if isinstance(properties, dict):
            # setdefault: a call site that already declared a budget keeps it.
            properties.setdefault("resilience", {"timeout_s": None})

    setattr(__init__, "jiuwenswarm_card_exempt", True)
    tool_cls.__init__ = __init__


def install_shell_tool_safety_hooks() -> None:
    """Idempotently wire safety checks into harness shell tools."""
    global _installed
    if _installed:
        return

    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    _patch_tool_class(BashTool)
    _stamp_exempt_tool_card(BashTool)

    try:
        from openjiuwen.harness.tools.shell.powershell._tool import PowerShellTool

        _patch_tool_class(PowerShellTool)
        _stamp_exempt_tool_card(PowerShellTool)
    except ImportError:
        pass

    _installed = True


def reset_installed_flag() -> None:
    """Reset the installed flag so hooks can be re-applied (for testing)."""
    global _installed
    _installed = False


__all__ = ["install_shell_tool_safety_hooks", "reset_installed_flag"]
