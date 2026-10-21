# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Executable identities for the closed builtin observation fast path."""

from collections.abc import Callable
from pathlib import Path
from types import CodeType, FunctionType
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool
from openjiuwen.core.foundation.tool.base import _ToolMeta
from openjiuwen.core.runner.callback import decorator as callbacks
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.tools.cron import create_cron_tools
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.store import SessionGoalStore
from openjiuwen.harness.schema.interaction import InteractionPhase
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
from openjiuwen.harness.tools.skills.list_skill import ListSkillTool
from openjiuwen.harness.tools.goal import GetCurrentGoalTool
from openjiuwen.harness.tools import compression_recall
from openjiuwen.symphony.discovery import InstalledSkillsDirectoryToolkit
from jiuwenswarm.common.tool_display import extract_call_goal
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import HeartbeatRuntimeBridge
from jiuwenswarm.agents.harness.common.tools import acp_output_tools as acp
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import _CronToolsCronBackend, _RestrictedCronBackend
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronTools
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.timestamp_tool import convert_timestamp_to_utc8_time
from jiuwenswarm.agents.harness.common.rails.permissions.tool_binding import matches_bound_method, resolve_tool_binding
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import tool_search_args_valid
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import ToolInvocation

# LocalFunction exposes no public callable-identity API; pin the executable, not its name.
_TIMESTAMP_FUNC = convert_timestamp_to_utc8_time._func  # pylint: disable=protected-access
_ACP_DELEGATES = {name: getattr(acp, name) for name in ("read_terminal_output", "wait_for_terminal_exit")}
_RECALL_DELEGATE = compression_recall.recall_compressed_context
_SESSION_OBSERVATION_TOOLS = {
    "tool_search", "skill_index", "list_skill", "get_current_goal", "recall_compressed_context",
}


def _closure(func, factory, name):
    """Match the actual callable, never a declared name or __wrapped__ pointer."""
    code = factory.__code__
    for part in name.split("."):
        code = next(c for c in code.co_consts if isinstance(c, CodeType) and c.co_name == part)
    # Proxies can spoof __class__; isinstance alone does not prove executable identity.
    exact_function = type(func) is FunctionType  # pylint: disable=huawei-unidiomatic-typecheck
    if not exact_function or func.__code__ is not code or func.__globals__ is not factory.__globals__:
        raise ValueError("builtin callable mismatch")
    return dict(zip(code.co_freevars, (c.cell_contents for c in func.__closure__ or ()), strict=True))


def _method_matches(owner, expected, name):
    method = getattr(owner, name, None)
    # A subclass may change the method's downstream behavior even if the method is inherited.
    exact_owner = type(owner) is expected  # pylint: disable=huawei-unidiomatic-typecheck
    return exact_owner and matches_bound_method(
        method, expected_owner=owner, expected_func=getattr(expected, name)
    )


def _methods_match(owner: object, expected_type: type, method_names: tuple[str, ...]) -> bool:
    """Verify every method against the same concrete owner type."""
    return all(_method_matches(owner, expected_type, name) for name in method_names)


def _invoke_matches(resource, invoke_func=LocalFunction.invoke, *, invoke=None):
    # Tool's metaclass installs these SDK wrappers on every instance.
    invoke = resource.invoke if invoke is None else invoke
    for factory, name in (
        (callbacks.create_emit_after_decorator, "decorator.async_wrapper"),
        # No public equivalent identifies this SDK-installed executable wrapper.
        (callbacks._make_transform_io_decorator, "async_wrapper"),  # pylint: disable=protected-access
        (callbacks.create_emit_before_decorator, "decorator.async_wrapper"),
    ):
        invoke = _closure(invoke, factory, name)["func"]
    lifecycle = _closure(invoke, _ToolMeta.__call__, "_lifecycle_invoke")
    original = lifecycle["_original_invoke"]
    return lifecycle["instance"] is resource and matches_bound_method(
        original, expected_owner=resource, expected_func=invoke_func
    )


def _owned_rail(agent, expected):
    rails = agent.find_rails_by_type((expected,))
    # Concrete identity prevents a subclass from changing downstream behavior.
    # pylint: disable-next=huawei-unidiomatic-typecheck
    if len(rails) != 1 or type(rails[0]) is not expected or not agent.is_registered_rail(rails[0]):
        return None
    return rails[0]


def _resolve_owned_readonly_tool(
    invocation: ToolInvocation,
    owner_agent: Any,
    *,
    tool_type: type[Tool],
    rail_type: type[AgentRail],
    invoke_func: Callable[..., Any] = LocalFunction.invoke,
) -> tuple[Tool, AgentRail] | None:
    """Resolve the concrete tool, owner rail and unchanged SDK executor."""
    tool = resolve_tool_binding(invocation.ctx.agent, invocation.tool_name, tool_type)
    rail = _owned_rail(owner_agent, rail_type)
    if tool is None or rail is None or not _invoke_matches(tool, invoke_func):
        return None
    return tool, rail


def _skill_getter_matches(tool, rail) -> bool:
    # SkillUseRail.init has two equivalent lambdas; match the finite SDK code set.
    func = tool.get_skills
    codes = tuple(code for code in SkillUseRail.init.__code__.co_consts
                  if isinstance(code, CodeType) and code.co_name == "<lambda>" and code.co_freevars == ("self",))
    # Match the executable itself; proxies can spoof __class__.
    return bool(
        # pylint: disable-next=huawei-unidiomatic-typecheck
        type(func) is FunctionType and any(func.__code__ is code for code in codes)
        and func.__globals__ is SkillUseRail.init.__globals__
        and len(func.__closure__ or ()) == 1 and func.__closure__[0].cell_contents is rail
        and _method_matches(rail, SkillUseRail, "get_skills_for_session")
    )


def _session_observation_binding(invocation, session_id: str, agent) -> bool:
    # No public API proves these SDK executable/owner identities. Exact types reject subclasses and proxies.
    # pylint: disable=protected-access,huawei-unidiomatic-typecheck
    # The callback runs on DeepAgent's inner ReActAgent; only its live owner is trusted.
    callback_agent = invocation.ctx.agent
    if agent is None or not session_id:
        return False
    if callback_agent is not agent and callback_agent is not agent.react_agent:
        return False
    if callback_agent.ability_manager is not agent.ability_manager:
        return False
    if agent._bound_session_id != session_id or invocation.ctx.session.get_session_id() != session_id:
        return False
    if agent.phase is InteractionPhase.TERMINATED:
        return False
    name = invocation.tool_name
    if name == "tool_search":
        # Lazy imports avoid coupling permission module initialization to discovery rails.
        from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
        from openjiuwen.harness.tools.tool_discovery.bm25 import BM25ToolIndex
        from openjiuwen.harness.tools.tool_discovery.tool_search import ToolSearchTool
        from jiuwenswarm.agents.harness.common.rails.tool_retrieval_prompt_rail import ToolRetrievalPromptRail
        from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import ToolRetrievalToolkit

        _, search_args = extract_call_goal(invocation.tool_args)
        binding = _resolve_owned_readonly_tool(
            invocation, agent, tool_type=ToolSearchTool, rail_type=ProgressiveToolRail,
            invoke_func=ToolSearchTool.invoke,
        )
        if binding is not None:
            tool, rail = binding
            index = rail._tool_search_index
            return bool(
                tool_search_args_valid(search_args, progressive=True)
                and rail._owned_tool_cards.get(name) is tool.card
                and rail._tool_search_registry is agent.ability_manager
                and type(tool._result_limit) is int and 1 <= tool._result_limit <= 20
                and matches_bound_method(tool._search_tools, expected_owner=rail,
                                         expected_func=ProgressiveToolRail._search_tools)
                and _methods_match(rail, ProgressiveToolRail, (
                    "_search_tools", "_ensure_tool_search_index", "_get_current_tool_search_documents",
                    "_list_registered_deferred_tool_infos", "_rebuild_tool_search_index",
                    "_authorize_discovered_tools", "_get_discovered_tools", "_set_discovered_tools",
                    "_get_discovered_tool_fingerprints", "_append_trace",
                ))
                and rail._build_tool_schema is ProgressiveToolRail._build_tool_schema
                and (index is None or _method_matches(index, BM25ToolIndex, "search"))
            )
        binding = _resolve_owned_readonly_tool(
            invocation, agent, tool_type=LocalFunction, rail_type=ToolRetrievalPromptRail,
        )
        if binding is None:
            return False
        tool, rail = binding
        toolkit = rail._toolkit
        # Live Host providers belong to this attached toolkit; discovery never invokes tool_call.
        return bool(
            tool_search_args_valid(search_args, progressive=False)
            and type(toolkit) is ToolRetrievalToolkit and rail._agent in (agent, callback_agent)
            and toolkit._session_scope == session_id
            and _methods_match(toolkit, ToolRetrievalToolkit,
                               ("tool_search", "_render_listing", "_render_schema", "_live_records"))
            and matches_bound_method(tool._func, expected_owner=toolkit, expected_func=ToolRetrievalToolkit.tool_search)
        )
    if name == "skill_index":
        from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import SkillRetrievalPromptRail
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import SkillRetrievalToolkit

        rail = _owned_rail(agent, SkillRetrievalPromptRail)
        if rail is None:
            return False
        wrapper = rail._source_toolkit
        if type(wrapper) is not SkillRetrievalToolkit or wrapper._session_scope != session_id:
            return False
        toolkit = wrapper._index_toolkit
        if not _methods_match(
            toolkit, InstalledSkillsDirectoryToolkit, ("get_tools", "skill_index", "_execute", "_continue"),
        ):
            return False
        # The verified public factory supplies the concrete SDK executor without a private-module import.
        reference = InstalledSkillsDirectoryToolkit.get_tools(toolkit)[0]
        binding = _resolve_owned_readonly_tool(
            invocation, agent, tool_type=type(reference), rail_type=SkillRetrievalPromptRail,
            invoke_func=type(reference).invoke,
        )
        if binding is None:
            return False
        tool, rail = binding
        return bool(
            rail._agent in (agent, callback_agent) and rail._session_scope == session_id
            and rail._prompt_skillfs is wrapper._environment
            and toolkit._environment is wrapper._environment and not toolkit._closed
            and matches_bound_method(
                tool._func, expected_owner=toolkit, expected_func=InstalledSkillsDirectoryToolkit.skill_index,
            )
        )
    if name == "list_skill":
        binding = _resolve_owned_readonly_tool(
            invocation, agent, tool_type=ListSkillTool, rail_type=SkillUseRail,
            invoke_func=ListSkillTool.invoke,
        )
        if binding is None:
            return False
        tool, rail = binding
        args = invocation.tool_args
        return bool(
            isinstance(args, dict) and type(args.get("query", "")) is str
            and (not args.get("query", "").strip() or tool.list_skill_model is None)
            and rail._owned_tool_cards.get(name) is tool.card and _skill_getter_matches(tool, rail)
            and _methods_match(tool, ListSkillTool, ("_resolve_skills", "_dump_all_skills"))
        )
    if name == "get_current_goal":
        binding = _resolve_owned_readonly_tool(
            invocation, agent, tool_type=GetCurrentGoalTool, rail_type=TaskCompletionRail,
            invoke_func=GetCurrentGoalTool.invoke,
        )
        if binding is None:
            return False
        tool, rail = binding
        manager = tool._goal_manager
        if manager is not agent.goal_manager or rail._goal_manager is not manager or tool not in rail._goal_tools:
            return False
        if not _methods_match(manager, GoalManager, ("get", "get_store")):
            return False
        store = manager.get_store(session_id)
        return _method_matches(store, SessionGoalStore, "load") and store.session_id == session_id
    if name != "recall_compressed_context":
        return False
    binding = _resolve_owned_readonly_tool(
        invocation, agent, tool_type=compression_recall.CompressionRecallTool, rail_type=ContextProcessorRail,
        invoke_func=compression_recall.CompressionRecallTool.invoke,
    )
    if binding is None:
        return False
    tool, rail = binding
    return bool(
        rail._owned_tool_cards.get(name) is tool.card
        and compression_recall.recall_compressed_context is _RECALL_DELEGATE
        and tool._workspace_dir and rail.workspace is agent.deep_config.workspace
        and Path(tool._workspace_dir).resolve() == Path(rail.workspace.root_path).resolve()
    )


def trusted_readonly_binding(invocation, session_id: str, *, owner_agent=None) -> bool:
    """Prove current implementation and direct owner; any mismatch stays manual."""
    try:
        if invocation.tool_name in _SESSION_OBSERVATION_TOOLS:
            return _session_observation_binding(invocation, session_id, owner_agent)
        resource = resolve_tool_binding(invocation.ctx.agent, invocation.tool_name, LocalFunction)
        if resource is None or not _invoke_matches(resource):
            return False
        # Compare the currently installed callable with the verified implementation.
        name, func = invocation.tool_name, resource._func  # pylint: disable=protected-access
        if name == "convert_timestamp_to_utc8_time":
            return resource is convert_timestamp_to_utc8_time and func is _TIMESTAMP_FUNC
        if not session_id:
            return False
        if name.startswith("cron_"):
            method = name.removeprefix("cron_")
            backend = _closure(func, create_cron_tools, method + "_wrapper")["backend"]
            # Unwrap only this concrete delegating implementation, never a subclass.
            restricted = type(backend) is _RestrictedCronBackend  # pylint: disable=huawei-unidiomatic-typecheck
            if restricted:
                delegate = getattr(backend, method)
                # The private delegate is the actual receiver; no public identity API exists.
                inner = backend._inner  # pylint: disable=protected-access
                if not matches_bound_method(
                    delegate, expected_owner=inner, expected_func=getattr(_CronToolsCronBackend, method)
                ):
                    return False
                backend = inner
            # Verify the executable backend and its bound session, not declared tool metadata.
            return bool(
                _methods_match(backend, _CronToolsCronBackend, (method, "_with_route"))
                and _method_matches(backend._cron_tools, CronTools, method)  # pylint: disable=protected-access
                and backend._bound_context.session_id == session_id  # pylint: disable=protected-access
            )
        if name.startswith("heartbeat_"):
            closure = _closure(func, HeartbeatRuntimeBridge.build_tools, name.removeprefix("heartbeat_"))
            bridge = closure["self"]
            # The concrete service owns execution; the bridge has no public service-identity API.
            return bool(
                _method_matches(bridge, HeartbeatRuntimeBridge, "_send")
                and _method_matches(
                    bridge._service, HeartbeatRailRuntime, "handle_operation",  # pylint: disable=protected-access
                )
                and closure["context"].session_id == session_id
            )
        if name in _ACP_DELEGATES:
            closure = _closure(func, acp.get_tools, name + "_bound")
            return closure["session_id"] == session_id and getattr(acp, name) is _ACP_DELEGATES[name]
        return False
    except Exception:  # SDK implementation changes must never grant a fast path.
        return False
