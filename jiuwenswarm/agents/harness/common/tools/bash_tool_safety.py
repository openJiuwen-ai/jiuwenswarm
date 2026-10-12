# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply jiuwenswarm shell safety rules to openjiuwen BashTool / PowerShellTool.

The agent's primary shell tool is ``bash`` (openjiuwen ``BashTool``), not
``mcp_exec_command``.  Safety checks in ``command_tools`` only affect the latter
unless we hook the harness tools here.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

_installed = False

_COMPACT_BASH_DESCRIPTION = {
    "cn": (
        "执行 Bash 命令并返回输出。工作目录会在调用之间保留，"
        "但变量、函数和别名等 Shell 状态不会保留。此工具固定使用 Bash/POSIX 语法，"
        "不要传入 PowerShell 或 CMD 语法。"
    ),
    "en": (
        "Execute a command with Bash and return its output. "
        "The working directory persists between calls, but shell state such as "
        "variables, functions, and aliases does not. Use POSIX/Bash syntax; "
        "do not send PowerShell or CMD syntax to this tool."
    ),
}
_COMPACT_POWERSHELL_DESCRIPTION = {
    "cn": (
        "执行 PowerShell 命令并返回输出。工作目录会在调用之间保留，"
        "但变量、函数和别名等 Shell 状态不会保留。命令在非交互模式下运行，"
        "不要调用需要交互输入、交互编辑器或图形界面的命令。"
        "默认按 Windows PowerShell 5.1 语法编写：不要使用 `&&`、`||`、`?:`、`??` 或 `?.`；"
        "条件执行使用 `A; if ($?) { B }`。5.1 下避免对原生命令使用 `2>&1`，"
        "写文本文件时显式指定 UTF-8。使用 `$env:NAME` 访问环境变量；"
        "调用路径含空格的可执行文件时使用 `&`。"
    ),
    "en": (
        "Execute a PowerShell command and return its output. "
        "The working directory persists between calls, but shell state such as "
        "variables, functions, and aliases does not. Commands run non-interactively; "
        "do not invoke prompts, interactive editors, or GUI input. "
        "Write commands compatible with Windows PowerShell 5.1 unless the runtime "
        "is known to use PowerShell 7+. Do not use `&&`, `||`, `?:`, `??`, or `?.` "
        "in 5.1; use `A; if ($?) { B }` for conditional execution. "
        "Avoid `2>&1` on native executables in 5.1 and specify UTF-8 when writing "
        "text files. Use `$env:NAME` for environment variables and `&` to invoke "
        "an executable whose path contains spaces."
    ),
}


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


def _remove_model_facing_shell_params(tool: Any) -> None:
    """Remove shell inputs that Swarm does not expose to the model."""
    card = getattr(tool, "card", None)
    params = getattr(card, "input_params", None) if card is not None else None
    if not isinstance(params, dict):
        return
    properties = params.get("properties")
    if isinstance(properties, dict):
        for name in ("description", "run_in_background", "background"):
            properties.pop(name, None)


def _ensure_bash_background_card(tool: Any, language: str) -> None:
    """Normalize the model-facing Bash card without changing execution behavior."""
    card = getattr(tool, "card", None)
    if card is None:
        return
    _remove_model_facing_shell_params(tool)
    lang = language if language in _COMPACT_BASH_DESCRIPTION else "cn"
    params = getattr(card, "input_params", None)
    if isinstance(params, dict):
        properties = params.setdefault("properties", {})
        if isinstance(properties, dict):
            # The BashTool wrapper fixes shell_type to "bash" before invoking
            # the upstream tool. Do not expose a model-facing selector that
            # cannot affect execution.
            properties.pop("shell_type", None)
    card.description = _COMPACT_BASH_DESCRIPTION[lang]


def _ensure_powershell_background_card(tool: Any, language: str) -> None:
    """Normalize the model-facing PowerShell card without changing execution behavior."""
    card = getattr(tool, "card", None)
    if card is None:
        return
    _remove_model_facing_shell_params(tool)
    lang = language if language in _COMPACT_POWERSHELL_DESCRIPTION else "cn"
    card.description = _COMPACT_POWERSHELL_DESCRIPTION[lang]


def _wrap_card_init(
    original: Callable[..., None],
    ensure_fn: Callable[[Any, str], None],
) -> Callable[..., None]:
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        language = "cn"
        try:
            bound = inspect.signature(original).bind(self, *args, **kwargs)
            bound.apply_defaults()
            raw_language = bound.arguments.get("language", "cn")
            if isinstance(raw_language, str):
                language = raw_language
        except TypeError:
            raw_language = kwargs.get("language")
            if isinstance(raw_language, str):
                language = raw_language
        ensure_fn(self, language)

    init.jiuwenswarm_card_wrapped = True  # type: ignore[attr-defined]
    return init


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
        routed_inputs.pop("run_in_background", None)
        routed_inputs.pop("background", None)
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
    already_card_wrapped = getattr(tool_cls.__init__, "jiuwenswarm_card_wrapped", False)
    if tool_name == "bash" and not already_card_wrapped:
        tool_cls.__init__ = _wrap_card_init(tool_cls.__init__, _ensure_bash_background_card)
    elif tool_name == "powershell" and not already_card_wrapped:
        tool_cls.__init__ = _wrap_card_init(
            tool_cls.__init__, _ensure_powershell_background_card
        )


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
    """Idempotently wire safety checks into harness shell tools.

    Imports the tool classes. Startup uses ``register_shell_safety_import_hook``
    so the import waits until something actually loads BashTool.
    """
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


def _on_bash_tool_loaded(module: Any) -> None:
    _patch_tool_class(module.BashTool, "bash")
    _patch_shell_execution_plan()
    global _installed
    _installed = True


def _on_powershell_tool_loaded(module: Any) -> None:
    _patch_tool_class(module.PowerShellTool, "powershell")


def register_shell_safety_import_hook() -> None:
    """Patch BashTool / PowerShellTool when their modules load.

    Does not import those modules. A command that loads the class therefore
    runs with the safety wrapper already installed.
    """
    from jiuwenswarm.common.import_hook import register_post_import_hook

    register_post_import_hook(
        "openjiuwen.harness.tools.shell.bash._tool",
        _on_bash_tool_loaded,
    )
    register_post_import_hook(
        "openjiuwen.harness.tools.shell.powershell._tool",
        _on_powershell_tool_loaded,
    )


def reset_installed_flag() -> None:
    """Reset the installed flag so hooks can be re-applied (for testing)."""
    global _installed
    _installed = False


__all__ = [
    "_remove_model_facing_shell_params",
    "_ensure_bash_background_card",
    "_ensure_powershell_background_card",
    "_patch_tool_class",
    "_pre_execute_shell_command",
    "_shell_mismatch",
    "_wrap_invoke",
    "install_shell_tool_safety_hooks",
    "register_shell_safety_import_hook",
    "reset_installed_flag",
]
