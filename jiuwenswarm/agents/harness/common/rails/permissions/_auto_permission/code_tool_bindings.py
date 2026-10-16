# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Closed Code control/LSP routes, verified against executable SDK bindings."""

from pathlib import Path

from pydantic import TypeAdapter
from openjiuwen.harness.rails.lsp_rail import LspRail
from openjiuwen.harness.rails.agent_mode_rail import AgentModeRail
from openjiuwen.harness.tools.agent_mode_tools import EnterPlanModeTool, ExitPlanModeTool, SwitchModeTool
from openjiuwen.harness.tools.lsp_tool._tool import LspTool
from openjiuwen.harness.tools.lsp_tool._schemas import LspToolInput
from openjiuwen.harness.security.permission_engine.fileguard.file_tool_specs import (
    FileToolSpec, lookup_file_tool_specs, register_file_tool,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_binding import resolve_tool_binding, matches_bound_method
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.readonly_tool_bindings import (
    _invoke_matches, _closure,
)

_CONTROL_TYPES = {
    "enter_plan_mode": EnterPlanModeTool, "exit_plan_mode": ExitPlanModeTool,
    "switch_mode": SwitchModeTool,
}
_LSP_INPUT = TypeAdapter(LspToolInput)


def register_lsp_read_semantics() -> None:
    """Teach the existing Core file guard about LSP's source path."""
    spec = FileToolSpec("lsp", "file_path", "read")
    if spec not in (lookup_file_tool_specs("lsp") or []):
        register_file_tool(spec)


def _control_invoke_matches(tool, expected) -> bool:
    invoke = tool.invoke
    # The SDK exposes no public identity API for its patched control wrappers.
    factory = {
        EnterPlanModeTool: AgentModeRail._patch_enter_plan_mode_tool,  # pylint: disable=protected-access
        ExitPlanModeTool: AgentModeRail._patch_exit_plan_mode_tool,  # pylint: disable=protected-access
    }.get(expected)
    if factory is not None and getattr(invoke, "__name__", "") == "patched_invoke":
        _closure(invoke, factory, "patched_invoke")
        defaults = getattr(invoke, "__defaults__", ())
        if len(defaults or ()) != 1:
            return False
        invoke = defaults[0]
    return _invoke_matches(tool, expected.invoke, invoke=invoke)


def trusted_code_route(invocation, facts, *, path_requires_approval: bool, owner_agent) -> str | None:
    """Return a closed route only after policy/guard denials have been applied."""
    try:
        agent = owner_agent
        callback_agent = invocation.ctx.agent
        # DeepAgent registers tool callbacks on its inner ReActAgent. Prove
        # that bridge against the rail owner, rather than trusting callback metadata.
        if agent is None or not (
            callback_agent is agent or callback_agent is agent.react_agent
        ) or callback_agent.ability_manager is not agent.ability_manager:
            return None
        if getattr(agent, "_jiuwenswarm_adapter_mode", None) != "code":
            return None
        expected = _CONTROL_TYPES.get(invocation.tool_name)
        if expected is not None:
            if not facts.arguments_valid_object:
                return None
            tool = resolve_tool_binding(callback_agent, invocation.tool_name, expected)
            # This private binding is the actual SDK receiver, not tool metadata.
            owns_agent = tool is not None and tool._agent_ref is agent  # pylint: disable=protected-access
            if owns_agent and _control_invoke_matches(tool, expected):
                return "code_plan_control"
            return None
        if invocation.tool_name != "lsp" or path_requires_approval:
            return None
        # Validate the actual discriminated SDK input; unknown operations stay manual.
        parsed = _LSP_INPUT.validate_python(dict(facts.untrusted_args), strict=True)
        if not facts.arguments_valid_object or facts.external_paths or facts.write_paths:
            return None
        tool = resolve_tool_binding(callback_agent, "lsp", LspTool)
        if tool is None or not _invoke_matches(tool, LspTool.invoke):
            return None
        # Pin the SDK's real workspace callback; it has no public identity API.
        workspace_getter = tool._get_workspace  # pylint: disable=protected-access
        expected_getter = LspTool._get_workspace  # pylint: disable=protected-access
        if not matches_bound_method(workspace_getter, expected_owner=tool, expected_func=expected_getter):
            return None
        root = Path(facts.workspace_root).resolve()
        if workspace_getter() != root or tool.operation is not agent.deep_config.sys_operation:
            return None
        rails = agent.find_rails_by_type(LspRail)
        if len(rails) != 1:
            return None
        # Subclasses can alter execution; assignability does not prove identity.
        exact_rail = type(rails[0]) is LspRail  # pylint: disable=huawei-unidiomatic-typecheck
        if not exact_rail:
            return None
        rail = rails[0]
        cwd = getattr(getattr(rail, "_initialize_options", None), "cwd", None)
        # Verify the tool installed by this concrete SDK rail.
        registered_tool = rail._lsp_tool  # pylint: disable=protected-access
        if registered_tool is not tool or not cwd or Path(cwd).resolve() != root:
            return None
        # workspaceSymbol reads the bound root; all other operations read one source file.
        if parsed.operation != "workspaceSymbol" and not facts.read_paths:
            return None
        if not facts.accesses_known or any(not Path(path).resolve().is_relative_to(root) for path in facts.read_paths):
            return None
        return "code_lsp_read"
    except (AttributeError, LookupError, OSError, RuntimeError, StopIteration, TypeError, ValueError):
        return None
