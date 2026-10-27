# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Closed session observations use real SDK tools, registrations and owner rails."""

import asyncio
from copy import deepcopy
from types import FunctionType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolExposure, ToolInfo
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.runner.resources_manager.resource_manager import ResourceMgr
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.store import SessionGoalStore
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.tools import compression_recall
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import readonly_tool_bindings as bindings
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import _extract_invocation
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import terminal_internal_route
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import classify_permission_result
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import PolicyEvaluation
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import classify_tool
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import SkillRetrievalPromptRail
from jiuwenswarm.agents.harness.common.rails.tool_retrieval_prompt_rail import ToolRetrievalPromptRail
from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import SkillRetrievalToolkit
from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import ToolRetrievalToolkit
from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _rail, _runtime_ctx


CASES = [
    ("tool_search", {"query": "get_current_goal"}),
    ("skill_index", {"operation": "search", "query": ["python"]}),
    ("list_skill", {"query": "python"}),
    ("get_current_goal", {}),
    ("recall_compressed_context", {"query": "previous result", "memory_id": "archive-1"}),
]


@pytest_asyncio.fixture
async def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(Runner, "resource_mgr", ResourceMgr())
    owners, toolkits = [], []

    async def build(name, args, *, model=None, progressive=True):
        owner = DeepAgent(AgentCard(id=f"observation-{len(owners)}", name="observation"))
        owner.configure(DeepAgentConfig(
            workspace=Workspace(root_path=tmp_path), auto_create_workspace=False,
            enable_read_image_multimodal=False, language="en",
        ))
        owners.append(owner)
        session = create_agent_session(session_id="session-a", card=owner.card)
        await session.pre_run(inputs={})
        owner._bound_session_id = session.get_session_id()
        if name == "tool_search" and progressive:
            rail = ProgressiveToolRail(owner.deep_config)
        elif name == "tool_search":
            toolkit = ToolRetrievalToolkit(deferred_tools_provider=lambda: [], session_scope="session-a")
            tool = toolkit.get_tools()[0]
            owner.ability_manager.add_ability(tool.card, tool)
            rail = ToolRetrievalPromptRail(toolkit=toolkit)
        elif name == "skill_index":
            toolkit = SkillRetrievalToolkit(
                skill_directories=[], session_scope="session-a", artifact_root=tmp_path / "index",
            )
            toolkits.append(toolkit)
            tool = toolkit.get_tools()[0]
            owner.ability_manager.add_ability(tool.card, tool)
            rail = SkillRetrievalPromptRail(toolkit=toolkit, session_scope="session-a")
        elif name == "list_skill":
            rail = SkillUseRail(str(tmp_path / "skills"), include_tools=False, list_skill_model=model)
        elif name == "get_current_goal":
            owner.goal_manager = GoalManager(
                store=SessionGoalStore(session), event_manager=owner.event_manager, control_lock=asyncio.Lock(),
                has_output_stream=lambda: False, cancel_active_round=AsyncMock(),
                emit_event=lambda event: None, notify_work=lambda: None,
            )
            rail = TaskCompletionRail()
        else:
            rail = ContextProcessorRail(preset=False)
        await owner.register_rail(rail)
        if name == "recall_compressed_context":
            # Use the SDK registration path without starting compressor/model workers.
            rail._register_compression_recall_tool(owner)
        card = owner.ability_manager.get(name)
        tool = Runner.resource_mgr.get_tool(card.id, session=None)
        ctx = _runtime_ctx(session, tool_name=name, tool_args=args)
        ctx.agent = owner.react_agent
        return SimpleNamespace(owner=owner, rail=rail, tool=tool, ctx=ctx)

    yield build
    for owner in owners:
        for rail in list(owner.configured_rails()):
            await owner.unregister_rail(rail)
        owner.ability_manager.teardown_tools()
    for toolkit in toolkits:
        toolkit._index_toolkit.close()


def _trusted(env):
    return bindings.trusted_readonly_binding(
        _extract_invocation((env.ctx,), {}), "session-a", owner_agent=env.owner,
    )


@pytest.mark.parametrize(("name", "args"), CASES)
@pytest.mark.parametrize("level", ["ask", "allow"])
@pytest.mark.asyncio
async def test_current_owned_observation_bypasses_reviewer_and_manual(tmp_path, installed, name, args, level):
    env = await installed(name, args)
    assert _trusted(env)
    auto, policy, reviewer, base = _rail(tmp_path, PolicyEvaluation(level=level, reason="configured"))
    auto.init(env.owner)
    assert await auto.before_tool_call(env.ctx) is None
    assert len(policy.calls) == 1 and reviewer.requests == [] and base.calls == []


@pytest.mark.parametrize(("name", "args"), CASES)
@pytest.mark.parametrize("defect", [
    "missing_owner", "bridge", "session", "cold", "stale", "duplicate_rail", "card", "shadow", "invoke", "delegate",
])
@pytest.mark.asyncio
async def test_unverified_observations_stay_manual(tmp_path, installed, monkeypatch, name, args, defect):
    env = await installed(name, args)
    assert _trusted(env)
    auto, _, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="allow", reason="configured"))
    auto.init(env.owner)
    if defect == "missing_owner":
        auto.uninit(env.owner)
    elif defect == "bridge":
        env.ctx.agent = SimpleNamespace(ability_manager=env.owner.ability_manager)
    elif defect == "session":
        env.owner._bound_session_id = "different-session"
    elif defect == "cold":
        env.owner._bound_session_id = None
    elif defect == "stale":
        env.owner._registered_rails.remove(env.rail)
        env.owner._pending_rails.append(env.rail)
    elif defect == "duplicate_rail":
        env.owner._pending_rails.append(env.rail)
    elif defect == "card":
        monkeypatch.setattr(env.tool, "_card", deepcopy(env.tool.card))
    elif defect == "shadow":
        resource = LocalFunction(card=env.tool.card, func=lambda **kwargs: "shadow")
        monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda *a, **kw: resource)
    elif defect == "invoke":
        monkeypatch.setattr(env.tool, "invoke", AsyncMock(return_value="rebound"))
    elif name == "tool_search":
        monkeypatch.setattr(env.rail, "_build_tool_schema", lambda *a: "rebound")
    elif name == "skill_index":
        monkeypatch.setattr(env.rail._source_toolkit._index_toolkit, "_execute", lambda *a: "rebound")
    elif name == "list_skill":
        monkeypatch.setattr(env.rail, "get_skills_for_session", lambda *a: [])
    elif name == "get_current_goal":
        monkeypatch.setattr(env.owner.goal_manager, "get", AsyncMock())
    else:
        monkeypatch.setattr(compression_recall, "recall_compressed_context", lambda **kw: {})
    with pytest.raises(AbortError):
        await auto.before_tool_call(env.ctx)
    assert reviewer.requests == []


@pytest.mark.parametrize(("name", "args"), CASES)
@pytest.mark.parametrize("gate", ["deny", "fail_closed", "explicit_human"])
@pytest.mark.asyncio
async def test_observation_preserves_earlier_gates(tmp_path, installed, name, args, gate):
    env = await installed(name, args)
    auto, _, reviewer, base = _rail(tmp_path, PolicyEvaluation(
        level="deny" if gate == "deny" else "allow", reason="configured",
        source="fail_closed" if gate == "fail_closed" else "engine",
        manual_approval_required=gate == "explicit_human",
    ))
    auto.init(env.owner)
    if gate == "explicit_human":
        assert await auto.before_tool_call(env.ctx) is None
        assert len(base.calls) == 1
    elif gate == "fail_closed":
        with pytest.raises(AbortError):
            await auto.before_tool_call(env.ctx)
    else:
        assert classify_permission_result(await auto.before_tool_call(env.ctx)) == "denied"
    assert reviewer.requests == []


@pytest.mark.parametrize("query,local", [("", True), ("  ", True), ("python", False)])
@pytest.mark.asyncio
async def test_list_skill_with_model_only_allows_local_branch(tmp_path, installed, query, local):
    model = SimpleNamespace(invoke=AsyncMock())
    env = await installed("list_skill", {"query": query}, model=model)
    auto, _, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    assert _trusted(env) is local
    if local:
        assert await auto.before_tool_call(env.ctx) is None
        result = await env.tool.invoke({"query": query}, session=env.ctx.session)
        assert result.success and result.data["mode"] == "all"
    else:
        with pytest.raises(AbortError):
            await auto.before_tool_call(env.ctx)
    model.invoke.assert_not_called()
    assert reviewer.requests == []


@pytest.mark.parametrize("defect", [
    "getter_code", "getter_globals", "store_load", "store_session", "workspace",
    "directory_factory", "directory_subclass",
])
@pytest.mark.asyncio
async def test_observation_rejects_rebound_session_dependencies(installed, monkeypatch, tmp_path, defect):
    name = "list_skill" if defect.startswith("getter") else "get_current_goal" if defect.startswith("store") else "recall_compressed_context"
    if defect.startswith("directory"):
        name = "skill_index"
    env = await installed(name, {})
    assert _trusted(env)
    if defect == "directory_factory":
        monkeypatch.setattr(env.rail._source_toolkit._index_toolkit, "get_tools", lambda: [env.tool])
    elif defect == "directory_subclass":
        class ChangedDirectoryTool(type(env.tool)):
            pass

        shadow = ChangedDirectoryTool(card=env.tool.card, func=env.tool._func)
        monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda *args, **kwargs: shadow)
    elif defect.startswith("getter"):
        func = env.tool.get_skills
        if defect == "getter_code":
            monkeypatch.setattr(env.tool, "get_skills", lambda session=None: [])
        else:
            monkeypatch.setattr(env.tool, "get_skills", FunctionType(
                func.__code__, {}, closure=func.__closure__, argdefs=func.__defaults__,
            ))
    elif defect == "store_load":
        monkeypatch.setattr(env.owner.goal_manager._store, "load", lambda: None)
    elif defect == "store_session":
        monkeypatch.setattr(env.owner.goal_manager._store, "_session", SimpleNamespace(get_session_id=lambda: "other"))
    else:
        monkeypatch.setattr(env.tool, "_workspace_dir", str(tmp_path / "other"))
    assert not _trusted(env)


VALID_ARGUMENTS = [
    ("tool_search", {}), ("tool_search", {"operation": "get_schema", "tool": "kill_app"}),
    ("tool_search", {"operation": "get_schema", "tools": ["powershell"]}),
    ("tool_search", {"call_goal": "discover", "query": ["close application"]}),
    ("tool_search", {"operation": "search", "query": "close application", "cursor": "old"}),
    ("skill_index", {"operation": "list"}), ("skill_index", {"operation": "search", "query": "python"}),
    ("skill_index", {"operation": "search", "cursor": "next"}),
    ("list_skill", {}), ("list_skill", {"query": ""}), ("get_current_goal", {}),
    ("recall_compressed_context", {"query": "context"}),
]
INVALID_ARGUMENTS = [
    ("tool_search", {"operation": "execute", "tool": "powershell"}),
    ("tool_search", {"operation": "get_schema"}), ("tool_search", {"operation": 1}),
    ("tool_search", {"operation": "list", "arguments": {}}),
    ("tool_search", {"query": [1]}), ("tool_search", {"tools": "powershell"}),
    ("tool_search", {"query": "x" * 4097}),
    ("skill_index", {"operation": "read", "skills": ["python"]}),
    ("skill_index", {"operation": "list", "paths": ["/tmp"]}),
    ("skill_index", {"operation": "list", "limit": 1}),
    ("skill_index", {"operation": "list", "pipeline": True}),
    ("skill_index", {"operation": "search", "query": []}),
    ("skill_index", {"operation": "search", "query": ["x"] * 9}),
    ("skill_index", {"operation": "search", "query": ["x" * 513]}),
    ("skill_index", {"operation": "list", "cursor": "x" * 129}),
    ("list_skill", {"query": None}), ("list_skill", {"query": "x" * 4097}),
    ("list_skill", {"query": "x", "model": "other"}), ("get_current_goal", {"session_id": "other"}),
    ("recall_compressed_context", {"query": ""}), ("recall_compressed_context", {"query": "x" * 4097}),
    ("recall_compressed_context", {"query": "x", "memory_id": "../other"}),
    ("recall_compressed_context", {"query": "x", "memory_id": "x" * 129}),
    ("recall_compressed_context", {"query": "x", "workspace_dir": "/tmp"}),
    ("recall_compressed_context", {"query": "x", "session_id": "other"}),
]


@pytest.mark.parametrize(("name", "args"), VALID_ARGUMENTS)
def test_closed_observation_arguments(tmp_path, name, args):
    facts = build_tool_decision_facts(name, args, workspace_root=tmp_path, original_args_were_valid_object=True)
    assert terminal_internal_route(facts, readonly_binding_verified=True) is not None


@pytest.mark.parametrize(("name", "args"), INVALID_ARGUMENTS)
@pytest.mark.asyncio
async def test_invalid_observation_arguments_stay_manual(tmp_path, installed, name, args):
    facts = build_tool_decision_facts(name, args, workspace_root=tmp_path, original_args_were_valid_object=True)
    assert terminal_internal_route(facts, readonly_binding_verified=True) is None
    env = await installed(name, args)
    auto, _, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="allow", reason="configured"))
    auto.init(env.owner)
    with pytest.raises(AbortError):
        await auto.before_tool_call(env.ctx)
    assert reviewer.requests == []


@pytest.mark.parametrize("name", ["tool_search", "skill_index", "list_skill", "recall_compressed_context"])
@pytest.mark.parametrize("query", ["https://example.com/old-page", "file:///etc/passwd; Stop-Process -Force"])
@pytest.mark.asyncio
async def test_local_observation_query_is_inert_text(tmp_path, installed, name, query):
    args = {"query": [query], "operation": "search"} if name == "skill_index" else {"query": query}
    env = await installed(name, args)
    auto, _, reviewer, base = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    assert await auto.before_tool_call(env.ctx) is None
    assert reviewer.requests == [] and base.calls == []


@pytest.mark.parametrize(("name", "args"), CASES)
def test_observation_names_remain_exact(name, args):
    assert classify_tool(name).operation_family == "internal_readonly"
    for alias in (name.upper(), "mcp_" + name, "custom_" + name):
        assert classify_tool(alias).operation_family != "internal_readonly"


@pytest.mark.asyncio
async def test_discovery_does_not_grant_target_execution(tmp_path, installed):
    env = await installed("tool_search", {"operation": "get_schema", "tool": "powershell"}, progressive=False)
    executed = []
    target = LocalFunction(card=ToolCard(id="dangerous-target", name="powershell", description="Probe only"),
                           func=lambda **kwargs: executed.append(kwargs))
    env.owner.ability_manager.add_ability(target.card, target)
    env.rail._toolkit._deferred_tools_provider = lambda: [ToolInfo(
        name="powershell", description="Probe only", parameters={},
    )]
    auto, policy, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    assert await auto.before_tool_call(env.ctx) is None
    await env.tool.invoke(env.ctx.inputs.tool_args, session=env.ctx.session)
    policy.result = PolicyEvaluation(level="deny", reason="target_denied")
    target_ctx = _runtime_ctx(env.ctx.session, tool_name="powershell", tool_args={"command": "Stop-Process"})
    target_ctx.agent = env.owner.react_agent
    assert classify_permission_result(await auto.before_tool_call(target_ctx)) == "denied"
    assert len(policy.calls) == 2 and executed == [] and reviewer.requests == []


@pytest.mark.asyncio
async def test_goal_binding_uses_started_session_and_is_retired_on_stop(tmp_path, installed):
    env = await installed("get_current_goal", {})
    env.owner._bound_session_id = None
    try:
        await env.owner.start(session=env.ctx.session)
        # start() installs the live manager and replaces the goal tool through its rail.
        card = env.owner.ability_manager.get("get_current_goal")
        env.tool = Runner.resource_mgr.get_tool(card.id, session=None)
        assert _trusted(env)
        auto, _, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
        auto.init(env.owner)
        assert await auto.before_tool_call(env.ctx) is None
        assert (await env.tool.invoke({}, session=env.ctx.session))["has_goal"] is False
        assert reviewer.requests == []
    finally:
        await env.owner.stop()
    assert not _trusted(env)


@pytest.mark.parametrize("args,allowed", [
    ({"query": "get_current_goal"}, True), ({"query": ""}, True),
    ({"query": "goal", "call_goal": "Find the goal tool"}, True),
    ({"query": "goal", "callGoal": "Find the goal tool"}, True),
    ({"query": "goal", "limit": 1, "call_goal": "Find the goal tool"}, True),
    ({"query": "goal", "limit": 20, "callGoal": "Find the goal tool"}, True),
    ({"query": "get_current_goal", "limit": None}, True),
    ({"query": "get_current_goal", "limit": 1}, True),
    ({"query": "get_current_goal", "limit": 20}, True),
    ({"query": "x" * 4096, "limit": 5}, True),
    ({}, False), ({"query": None}, False), ({"query": ["goal"]}, False),
    ({"query": "goal", "limit": True}, False), ({"query": "goal", "limit": 1.0}, False),
    ({"query": "goal", "limit": "5"}, False), ({"query": "goal", "limit": 0}, False),
    ({"query": "goal", "limit": 21}, False), ({"query": "x" * 4097}, False),
    ({"query": "bad\x00query"}, False), ({"query": "goal", "operation": "search"}, False),
    ({"query": "goal", "tool": "powershell"}, False),
])
@pytest.mark.asyncio
async def test_sdk_search_uses_only_its_registered_contract(tmp_path, installed, args, allowed):
    env = await installed("tool_search", args)
    assert _trusted(env) is allowed
    auto, _, reviewer, base = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    if allowed:
        assert await auto.before_tool_call(env.ctx) is None
        output = await env.tool.invoke(args, session=env.ctx.session)
        assert output.success
        assert output.data["count"] <= (args.get("limit") or 5)
    else:
        with pytest.raises(AbortError):
            await auto.before_tool_call(env.ctx)
    assert reviewer.requests == [] and base.calls == []


@pytest.mark.parametrize("args,allowed", [
    ({}, True), ({"operation": "list"}, True),
    ({"operation": "get_schema", "tool": "powershell"}, True),
    ({"query": ["goal"]}, True), ({"query": "goal"}, True),
    ({"query": "goal", "limit": 1}, False),
    ({"operation": "search", "query": "goal", "limit": 1}, False),
])
@pytest.mark.asyncio
async def test_legacy_search_keeps_its_own_contract(tmp_path, installed, args, allowed):
    env = await installed("tool_search", args, progressive=False)
    assert _trusted(env) is allowed
    auto, _, reviewer, base = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    if allowed:
        assert await auto.before_tool_call(env.ctx) is None
    else:
        with pytest.raises(AbortError):
            await auto.before_tool_call(env.ctx)
    assert reviewer.requests == [] and base.calls == []


@pytest.mark.parametrize("defect", [
    "owned_card", "registry", "delegate_owner", "tool_subclass", "rail_subclass",
    "ensure", "authorize", "trace", "index_search", "default_limit",
])
@pytest.mark.asyncio
async def test_sdk_search_rejects_changed_dependencies(installed, monkeypatch, defect):
    env = await installed("tool_search", {"query": "goal"})
    assert _trusted(env)
    if defect == "owned_card":
        monkeypatch.setattr(env.rail, "_owned_tool_cards", {})
    elif defect == "registry":
        monkeypatch.setattr(env.rail, "_tool_search_registry", SimpleNamespace())
    elif defect == "delegate_owner":
        other = ProgressiveToolRail(env.owner.deep_config)
        monkeypatch.setattr(env.tool, "_search_tools", other._search_tools)
    elif defect == "tool_subclass":
        monkeypatch.setattr(env.tool, "__class__", type("SearchSubclass", (type(env.tool),), {}))
    elif defect == "rail_subclass":
        monkeypatch.setattr(env.rail, "__class__", type("RailSubclass", (type(env.rail),), {}))
    elif defect == "index_search":
        env.rail._ensure_tool_search_index()
        monkeypatch.setattr(env.rail._tool_search_index, "search", lambda *a, **kw: [])
    elif defect == "default_limit":
        monkeypatch.setattr(env.tool, "_result_limit", True)
    else:
        method = {"ensure": "_ensure_tool_search_index", "authorize": "_authorize_discovered_tools",
                  "trace": "_append_trace"}[defect]
        monkeypatch.setattr(env.rail, method, lambda *a, **kw: None)
    assert not _trusted(env)


@pytest.mark.asyncio
async def test_sdk_discovery_never_executes_the_discovered_target(tmp_path, installed):
    env = await installed("tool_search", {"query": "dangerous_target", "limit": 1})
    executed = []
    target = LocalFunction(card=ToolCard(
        id="dangerous-sdk-target", name="dangerous_target", description="Probe only",
        exposure=ToolExposure.DEFERRED,
    ), func=lambda **kwargs: executed.append(kwargs))
    env.owner.ability_manager.add_ability(target.card, target)
    auto, policy, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="ask", reason="configured"))
    auto.init(env.owner)
    assert await auto.before_tool_call(env.ctx) is None
    output = await env.tool.invoke(env.ctx.inputs.tool_args, session=env.ctx.session)
    assert output.success and output.data["results"][0]["name"] == "dangerous_target"
    assert executed == []
    policy.result = PolicyEvaluation(level="deny", reason="target_denied")
    target_ctx = _runtime_ctx(env.ctx.session, tool_name="dangerous_target", tool_args={})
    target_ctx.agent = env.owner.react_agent
    assert classify_permission_result(await auto.before_tool_call(target_ctx)) == "denied"
    assert executed == [] and reviewer.requests == [] and len(policy.calls) == 2
