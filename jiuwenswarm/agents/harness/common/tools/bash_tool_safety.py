# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply jiuwenswarm shell safety rules to openjiuwen BashTool / PowerShellTool.

The agent's primary shell tool is ``bash`` (openjiuwen ``BashTool``), not
``mcp_exec_command``.  Safety checks in ``command_tools`` only affect the latter
unless we hook the harness tools here.
"""

from __future__ import annotations

import re as _re
from typing import Any, Awaitable, Callable

_installed = False

# ---------------------------------------------------------------------------
# Output post-processing: replace BashTool's <persisted-output> head-only
# preview with an inline head+tail view so error messages at the end of
# large command output (e.g. pytest failures) are always visible.
# ---------------------------------------------------------------------------

_PERSISTED_PATH_RE = _re.compile(r"Full output saved to:\s*(.+?)(?:\n|$)")


def _shell_output_config() -> tuple[int, float]:
    """Return (max_chars, head_ratio) from config, with safe fallback."""
    try:
        from jiuwenswarm.common.config import get_config
        cfg = (get_config() or {}).get("shell_output") or {}
        return int(cfg.get("max_chars", 20000)), float(cfg.get("head_ratio", 0.6))
    except Exception:
        return 20000, 0.6


def _post_process_bash_output(tool_output: Any, max_chars: int, head_ratio: float) -> Any:
    """Replace a <persisted-output> block with an inline head+tail truncated view.

    BashTool persists output >max_output_chars to a temp file and shows only
    the first 2 KB (head only).  We read the persisted file and return a
    head+tail view instead so that error messages near the end are preserved.
    Falls back to the original output on any error.
    """
    try:
        from openjiuwen.harness.tools.base_tool import ToolOutput
        if not (
            isinstance(tool_output, ToolOutput)
            and tool_output.data
            and isinstance(tool_output.data.get("content"), str)
            and "<persisted-output>" in tool_output.data["content"]
        ):
            return tool_output

        match = _PERSISTED_PATH_RE.search(tool_output.data["content"])
        if not match:
            return tool_output

        filepath = match.group(1).strip()
        try:
            from openjiuwen.harness.tools.shell.bash._output import truncate_output
            with open(filepath, encoding="utf-8", errors="replace") as fh:
                full_content = fh.read()
            total_chars = len(full_content)
            truncated = truncate_output(full_content, max_chars, head_ratio=head_ratio)
            new_content = (
                f"[Output truncated — {total_chars:,} chars total; "
                f"head+tail shown below. Full output: {filepath}]\n\n"
                f"{truncated}"
            )
        except Exception:
            return tool_output  # cannot read file — keep original persisted-output block

        return ToolOutput(
            success=tool_output.success,
            data={"content": new_content},
            error=tool_output.error,
        )
    except Exception:
        return tool_output


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
        result = await original(self, inputs, **kwargs)
        max_chars, head_ratio = _shell_output_config()
        return _post_process_bash_output(result, max_chars, head_ratio)

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
        max_chars, head_ratio = _shell_output_config()
        async for item in original(self, inputs, **kwargs):
            # Final summary chunk has "content"; intermediate chunks have "text"/"type".
            if item.data and "content" in item.data:
                item = _post_process_bash_output(item, max_chars, head_ratio)
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
