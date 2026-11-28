"""Prevent cron execution sessions from dispatching tools to the host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.core.sys_operation import OperationMode

# These built-ins delegate to the session SysOperation; a supported sandbox
# binds the Gateway cron directory read-only for both shell and file tools.
_SYSOP_TOOLS = frozenset({
    "bash", "cmd", "powershell", "exec_command", "mcp_exec_command", "execute_bash",
    "run_command", "shell", "write_file", "edit_file", "code", "execute_code",
})
# Client terminal and file writes are outside the session SysOperation.
_EXTERNAL_TOOLS = frozenset({
    "write_text_file", "create_terminal", "apply_patch", "extract_archive",
    "write", "search_replace",
})


def _matches_tool(name: str, tools: frozenset[str]) -> bool:
    return any(name == tool or name.startswith(tool + "_") for tool in tools)


class CronStorageRail(AgentRail):
    """Require protected execution for tools that can change host files."""

    priority = 96

    def __init__(
        self,
        is_cron_session: Callable[[], bool],
        get_sys_operation_card: Callable[[], Any],
    ) -> None:
        super().__init__()
        self._is_cron_session = is_cron_session
        self._get_sys_operation_card = get_sys_operation_card

    def _protected_sys_operation(self) -> bool:
        card = self._get_sys_operation_card()
        if card is None or card.mode != OperationMode.SANDBOX:
            return False
        launcher = getattr(card.gateway_config, "launcher_config", None)
        if launcher is None or launcher.sandbox_type != "jiuwenbox":
            return False
        options = launcher.extra_params or {}
        if options.get("fallback_on_failure") or options.get("excluded_commands"):
            return False
        from agent_gateway.common.paths import get_gateway_cron_dir

        policy = options.get("policy") or {}
        if options.get("api_sandbox_runtime") == "conch":
            policy = policy.get("conch") or {}
        mounts = (policy.get("filesystem_policy") or {}).get("bind_mounts") or []
        directory = str(get_gateway_cron_dir().resolve())
        return any(
            mount.get("host_path") == directory
            and mount.get("sandbox_path") == directory
            and mount.get("mode") == "ro"
            for mount in mounts
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        name = inputs.get("tool_name") if isinstance(inputs, dict) else getattr(inputs, "tool_name", None)
        name = str(name or "").lower()
        external = _matches_tool(name, _EXTERNAL_TOOLS)
        if not (external or _matches_tool(name, _SYSOP_TOOLS)):
            return
        if not self._is_cron_session():
            return
        if not external and self._protected_sys_operation():
            return

        error = "Cron execution sessions require a protected sandbox for this tool."
        ctx.extra["_skip_tool"] = True
        result = {"success": False, "error": error}
        if isinstance(inputs, dict):
            inputs["tool_result"] = result
        else:
            inputs.tool_result = result
            if hasattr(inputs, "tool_msg"):
                inputs.tool_msg = ToolMessage(
                    content=error, tool_call_id=getattr(inputs.tool_call, "id", ""),
                )
