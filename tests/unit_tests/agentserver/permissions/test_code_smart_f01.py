# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F01 exercises the product Code Spec and actual SDK permission graph."""

import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperationCard
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.common.mode_matrix import supports_single_agent_permission_profile
from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import supports_phase_auto_root
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail, plan_hard_rejected,
)
from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
    CodeAgentModeRail, CodePlanPrePermissionGuardRail,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.permission_rail_group import PermissionRailGroup
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import _ColdBuild


@pytest.fixture
async def code_cold(tmp_path, monkeypatch):
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter.mark_as_session_scoped(f"f01-{tmp_path.name}")
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))
    h = _ColdBuild(adapter, tmp_path)
    h.raw["progressive_tool_enabled"] = False
    h.raw["modes"] = {"code": {"tools": [], "rails": []}}
    card = SysOperationCard(id=h.sysop.id, work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]))
    Runner.resource_mgr.remove_sys_operation(card.id)
    assert Runner.resource_mgr.add_sys_operation(card).is_ok()
    h.sysop = Runner.resource_mgr.get_sys_operation(card.id)
    adapter._sys_operation_card = card
    monkeypatch.setattr(interface_code, "get_config", lambda: deepcopy(h.raw))
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(h.raw))
    monkeypatch.setattr(interface_code, "build_code_system_prompt", lambda **kw: "Code prompt")
    monkeypatch.setattr(adapter, "_capture_permission_version", h.capture)
    monkeypatch.setattr(adapter, "_create_sys_operation", lambda: h.sysop)
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", Mock())
    monkeypatch.setattr(adapter, "_resolve_enable_read_image_multimodal", lambda _config: False)
    monkeypatch.setattr(adapter, "_ensure_cron_tools_registered", Mock())
    monkeypatch.setattr(adapter, "_build_configured_subagents", lambda *_args: ([], False))
    for name in ("set_checkpoint", "_load_active_packages", "load_user_rails", "_sync_code_graph",
                 "_sync_mcp_servers_for_runtime", "_try_init_a2x_client", "_handle_memory_rail_by_config",
                 "_sync_skill_retrieval_prompt_rail_for_runtime"):
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in ("_sync_symphony_tools_for_runtime", "_sync_paid_search_tool_for_runtime",
                 "_sync_skill_retrieval_tools_for_runtime", "_sync_active_evolution_review_agent_after_reload",
                 "_sync_a2x_runtime_state"):
        monkeypatch.setattr(adapter, name, Mock())
    # Preserve the product Spec/provider, plan guard and shared permission builders.
    for name in ("runtime_prompt", "response_prompt", "skill_retrieval_prompt", "symphony_orchestration",
                 "security", "heartbeat", "session_messaging_route", "lsp", "project_memory", "filesystem",
                 "agent_md_prompt", "coding_memory", "memory_forbidden", "confirm_interrupt", "context_processor",
                 "eternal_conversation", "code_task_planning", "code_agent", "design", "subagent"):
        method = "_build_lsp_rail_via_config" if name == "lsp" else f"_build_{name}_rail"
        monkeypatch.setattr(adapter, method, lambda **_kw: None)

    def model(_config):
        adapter._adopt_code_spec_model(h.model)
        return h.model

    async def snapshot(config, _env):
        adapter._config_base_cache = deepcopy(config)
        adapter._config_cache = deepcopy(config.get("react", {}))
        return config

    monkeypatch.setattr(adapter, "_create_model", model)
    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", snapshot)
    try:
        yield h
    finally:
        instance = adapter._instance
        if instance is not None:
            for rail in list(instance.configured_rails()):
                await instance.unregister_rail(rail)
            instance.ability_manager.teardown_tools()
        Runner.resource_mgr.remove_sys_operation(card.id)
        for tool in (h.tool, h.shared_tool):
            if Runner.resource_mgr.get_tool(tool.card.id) is not None:
                Runner.resource_mgr.remove_tool(tool.card.id)


async def _create(h, *, project=True, sub_mode="normal"):
    await h.adapter.create_instance(
        {"channel_id": "web", "project_dir": str(h.root) if project else None},
        mode="code", sub_mode=sub_mode,
    )


def _verify(adapter, smart=True):
    group = PermissionRailGroup(
        adapter._permission_rail, adapter._root_permission_queue_rail,
        adapter._root_context_rail, adapter._root_permission_completion_rail,
        adapter._stream_event_rail, adapter._code_ask_user_rail,
    )
    group.verify(adapter._instance, smart=smart, queue=adapter._root_permission_queue,
                 sys_operation=adapter._sys_operation)
    assert all(rail in adapter._code_spec_rails for rail in group.rails())


@pytest.mark.parametrize("project,sub_mode", [(True, "normal"), (True, "plan"), (False, "normal")])
async def test_code_spec_installs_verified_smart_graph_and_bound_cwd(code_cold, project, sub_mode):
    h = code_cold
    await _create(h, project=project, sub_mode=sub_mode)
    a = h.adapter
    assert isinstance(a._instance, DeepAgent)
    assert a._is_code_agent is True
    assert isinstance(a._permission_rail, AutoPermissionInterruptRail)
    assert a._permission_rail.sys_operation is a._instance.deep_config.sys_operation
    assert a._model is a._instance.deep_config.model
    assert a._permission_state.permission_epoch == "cold-1"
    assert a._instance.deep_config.cwd == str(a._require_permission_workspace_binding().cwd)
    assert a._instance.deep_config.project_root == str(a._permission_workspace_root)
    _verify(a)


async def test_permission_transitions_preserve_children_spec_and_external_rails(code_cold):
    h = code_cold
    await _create(h)
    a = h.adapter
    child = SubAgentConfig(agent_card=AgentCard(name="general-purpose"), system_prompt="custom child")
    children = [child]
    a._instance.deep_config.subagents = children
    original_children = a._instance.deep_config.subagents
    external = AgentRail()
    await a._instance.register_rail(external)
    spec, model, operation = a._code_agent_spec, a._model, a._sys_operation
    for enabled, mode in [(True, "manual"), (True, "auto"), (False, "auto"), (True, "auto")]:
        h.raw["permissions"].update(enabled=enabled, mode=mode)
        h.change()
        await a.reload_agent_config(h.raw, reload_scopes={"permissions"})
        smart = enabled and mode == "auto"
        assert a._enable_auto_permission is smart
        _verify(a, smart)
        assert a._instance.deep_config.subagents is original_children
        assert a._instance.deep_config.subagents[0] is child
        assert a._code_agent_spec is spec and a._model is model and a._sys_operation is operation
        assert a._instance.is_registered_rail(external)


async def test_full_code_reload_retains_spec_owner_and_publishes_final_group(code_cold):
    h = code_cold
    await _create(h)
    a = h.adapter
    instance, old_spec = a._instance, a._code_agent_spec
    external = AgentRail()
    await instance.register_rail(external)
    h.raw["react"]["agent_name"] = "updated-code"
    h.change()
    await a.reload_agent_config(h.raw, reload_scopes={"models", "permissions"})
    assert a._instance is instance and a._code_agent_spec is not old_spec
    assert a._model is instance.deep_config.model
    assert instance.is_registered_rail(external)
    assert a._permission_state.permission_epoch == "cold-2"
    _verify(a)


@pytest.mark.parametrize("kind", ["candidate", "registration", "full"])
async def test_code_reload_failure_keeps_old_group_or_isolates(code_cold, monkeypatch, kind):
    h = code_cold
    await _create(h)
    a = h.adapter
    old = a._permission_rail
    if kind == "registration":
        monkeypatch.setattr(a._instance, "register_rail", AsyncMock(side_effect=RuntimeError("failure")))
    else:
        monkeypatch.setattr(a, "_build_session_permission_group", Mock(side_effect=RuntimeError("failure")))
    h.change()
    if kind == "full":
        h.raw["react"]["agent_name"] = "force-resource-reload"
    with pytest.raises(RuntimeError, match="failure"):
        await a.reload_agent_config(h.raw, reload_scopes={"models"} if kind == "full" else {"permissions"})
    assert a._permission_state.permission_isolated is (kind != "candidate")
    if kind == "candidate":
        assert a._permission_rail is old
        _verify(a)
        assert a._permission_state.permission_epoch == "cold-1"


async def test_busy_code_keeps_old_policy_until_settled(code_cold):
    h = code_cold
    await _create(h)
    a = h.adapter
    h.change()
    a._mark_session_active(a._parent_session_id)
    try:
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await a.reload_agent_config(h.raw, reload_scopes={"permissions"})
        assert a._permission_state.permission_epoch == "cold-1"
    finally:
        a._unmark_session_active(a._parent_session_id)
    await a.reload_agent_config(h.raw, reload_scopes={"permissions"})
    assert a._permission_state.permission_epoch == "cold-2"


async def test_code_permission_notification_keyword_is_forwarded(monkeypatch):
    a = interface_code.JiuwenSwarmCodeAdapter()
    a._custom_code_spec_active = True
    config = {"react": {}}
    monkeypatch.setattr(a, "_apply_reload_config_snapshot", AsyncMock(return_value=config))
    fanout = AsyncMock()
    monkeypatch.setattr(a, "_fan_out_reload_to_session_adapters", fanout)
    await a._reload_agent_config(config, {}, permission_notification=True)
    assert fanout.call_args.kwargs["permission_notification"] is True


@pytest.mark.parametrize("mode,work,expected", [
    ("agent", "work", True), ("agent.plan", "code", True), ("code", None, True),
    ("code.normal", None, True), ("code.plan", None, True), ("agent.code", None, True),
    ("agent.code.normal", "code", True), ("agent.code.plan", None, True),
    ("agent.work.plan", "code", False), ("agent.code.normal", "work", False),
    ("code.team", None, False), ("team.code.plan", None, False), ("auto_harness", None, False),
    ("agent.unknown", None, False), ("code.fast", None, False), ("agent.fast", None, True),
])
def test_f01_request_and_profile_allowlist(mode, work, expected):
    assert supports_single_agent_permission_profile(mode, work_mode=work) is expected
    assert supports_phase_auto_root({"mode": mode, "work_mode": work}) is expected


async def test_plan_guard_precedes_permission_and_executes_once(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
        create_send_file_execution_grant, current_send_file_execution_grant,
        publish_send_file_execution_grant,
    )
    from jiuwenswarm.server.runtime.sandbox_no_host_fallback import (
        no_host_fallback_required, require_no_host_fallback,
    )
    mode = CodeAgentModeRail(allowed_tools=["bash"])
    mode._agent = SimpleNamespace(load_state=lambda _: SimpleNamespace(plan_mode=SimpleNamespace(mode="plan")))
    parent = AsyncMock()
    monkeypatch.setattr(CodeAgentModeRail.__bases__[0], "before_tool_call", parent)
    guard = CodePlanPrePermissionGuardRail(mode)
    ctx = SimpleNamespace(session=SimpleNamespace(), inputs=SimpleNamespace(
        tool_name="bash", tool_call=SimpleNamespace(id="blocked"), tool_args={"command": "mkdir src"}), extra={})
    await guard.before_tool_call(ctx)
    await mode.before_tool_call(ctx)
    assert plan_hard_rejected(ctx)
    parent.assert_awaited_once()
    permission = JiuwenSwarmPermissionInterruptRail.__new__(JiuwenSwarmPermissionInterruptRail)
    await permission.before_tool_call(ctx)  # No engine access: hard rejection is terminal.
    previous_file = tmp_path / "previous.txt"
    previous_file.write_text("previous call")
    publish_send_file_execution_grant(create_send_file_execution_grant((previous_file,)))
    require_no_host_fallback()
    auto = AutoPermissionInterruptRail.__new__(AutoPermissionInterruptRail)
    await auto.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True
    assert current_send_file_execution_grant() is None
    assert not no_host_fallback_required()


async def test_trusted_code_controls_require_actual_executable_and_owner(code_cold):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.code_tool_bindings import trusted_code_route
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts
    h = code_cold
    await _create(h)
    agent = h.adapter._instance
    for name in ("enter_plan_mode", "exit_plan_mode", "switch_mode"):
        invocation = SimpleNamespace(ctx=SimpleNamespace(agent=agent.react_agent), tool_name=name)
        facts = build_tool_decision_facts(name, {}, workspace_root=h.root, original_args_were_valid_object=True)
        assert trusted_code_route(invocation, facts, path_requires_approval=False, owner_agent=agent) == "code_plan_control"
        card = agent.ability_manager.get(name)
        tool = Runner.resource_mgr.get_tool(card.id)
        original = tool.invoke
        tool.invoke = AsyncMock()
        assert trusted_code_route(invocation, facts, path_requires_approval=False, owner_agent=agent) is None
        tool.invoke = original
        tool._agent_ref = object()
        assert trusted_code_route(invocation, facts, path_requires_approval=False, owner_agent=agent) is None
        tool._agent_ref = agent
        invocation.ctx.agent = SimpleNamespace(ability_manager=agent.ability_manager)
        assert trusted_code_route(invocation, facts, path_requires_approval=False, owner_agent=agent) is None
        invocation.ctx.agent = agent.react_agent
        assert trusted_code_route(invocation, facts, path_requires_approval=False, owner_agent=None) is None


@pytest.mark.parametrize("operation,args", [
    ("goToDefinition", {"line": 1}), ("findReferences", {"line": 1}),
    ("documentSymbol", {}), ("workspaceSymbol", {}), ("goToImplementation", {"line": 1}),
    ("prepareCallHierarchy", {"line": 1}), ("incomingCalls", {"line": 1}), ("outgoingCalls", {"line": 1}),
])
async def test_lsp_fast_route_requires_valid_bound_in_root_read(code_cold, monkeypatch, operation, args):
    from openjiuwen.harness.lsp import InitializeOptions
    from openjiuwen.harness.rails.lsp_rail import LspRail
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.code_tool_bindings import trusted_code_route
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts
    h = code_cold
    await _create(h)
    agent = h.adapter._instance
    monkeypatch.setattr(LspRail, "_start_lsp_initialization", Mock())
    lsp = LspRail(options=InitializeOptions(cwd=str(h.root)))
    await agent.register_rail(lsp)
    await agent.ensure_initialized()
    invocation = SimpleNamespace(ctx=SimpleNamespace(agent=agent.react_agent), tool_name="lsp")
    raw = {"operation": operation, "file_path": str(h.root / "source.py"), **args}

    def route(data=raw, path_ask=False):
        facts = build_tool_decision_facts("lsp", data, workspace_root=h.root, original_args_were_valid_object=True)
        return trusted_code_route(invocation, facts, path_requires_approval=path_ask, owner_agent=agent)

    assert route() == "code_lsp_read"
    invocation.ctx.agent = SimpleNamespace(ability_manager=agent.ability_manager)
    assert route() is None
    invocation.ctx.agent = agent.react_agent
    assert route(path_ask=True) is None
    assert route({**raw, "operation": "rename"}) is None
    if operation != "workspaceSymbol":
        assert route({**raw, "file_path": str(h.root.parent / "outside.py")}) is None
        if "line" in args:
            assert route({**raw, "line": 0}) is None
        symlink = h.root / "escape.py"
        symlink.symlink_to(h.root.parent / "outside.py")
        assert route({**raw, "file_path": str(symlink)}) is None
    class DerivedLspRail(LspRail):
        pass

    derived = DerivedLspRail(options=InitializeOptions(cwd=str(h.root)))
    derived._lsp_tool = lsp._lsp_tool
    with monkeypatch.context() as patch:
        patch.setattr(agent, "find_rails_by_type", lambda _rail_type: [derived])
        assert route() is None

    lsp._initialize_options.cwd = str(h.root.parent)
    assert route() is None


async def test_code_guard_and_explicit_deny_precede_fast_control_route(code_cold, tmp_path):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
    from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import PolicyEvaluation
    from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _rail, _Session
    h = code_cold
    await _create(h)
    rail, policy, reviewer, base = _rail(tmp_path, PolicyEvaluation(level="deny", reason="explicit-deny"))
    args = {"mode": "plan"}
    ctx = AgentCallbackContext(agent=h.adapter._instance, session=_Session(), inputs=ToolCallInputs(
        tool_name="switch_mode", tool_call=SimpleNamespace(id="deny-control", name="switch_mode", arguments=args),
        tool_args=args), extra={})
    await rail.before_tool_call(ctx)
    assert reviewer.requests == [] and base.calls == []
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.parametrize("initial,target,project", [("manual", "auto", False), ("auto", "manual", True)])
async def test_combined_permission_and_model_reload_uses_target_workspace(code_cold, initial, target, project):
    h = code_cold
    h.raw["permissions"]["mode"] = initial
    await _create(h, project=project)
    h.raw["permissions"]["mode"] = target
    h.raw["react"]["agent_name"] = "combined-reload"
    h.change()
    await h.adapter.reload_agent_config(h.raw, reload_scopes={"models", "permissions"})
    _verify(h.adapter, target == "auto")
    if target == "auto":
        assert h.adapter._instance.deep_config.workspace.root_path == str(h.adapter._permission_workspace_root)
        assert h.adapter._permission_rail.workspace_root == h.adapter._permission_workspace_root


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
async def test_code_cold_start_never_publishes_unverified_graph(code_cold, monkeypatch, failure):
    h = code_cold
    if failure == "missing":
        monkeypatch.setattr(interface_deep, "build_permission_rail", lambda **_kw: None)
    else:
        from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
        async def package():
            await h.adapter._instance.register_rail(StructuredAskUserRail())
        monkeypatch.setattr(h.adapter, "_load_active_packages", package)
    with pytest.raises(RuntimeError, match="required_|permission_registered_graph_mismatch"):
        await _create(h)
    assert h.adapter._permission_state.permission_epoch is None
    assert h.adapter._permission_state.permission_isolated
    assert h.adapter._permission_state.permission_cleanup_complete
    assert h.adapter.get_live_session_instance(h.adapter._parent_session_id) is None
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await h.adapter.ensure_instance()


async def test_code_caller_product_spec_keeps_borrowed_operation(code_cold):
    h = code_cold
    a = h.adapter
    a._project_dir = str(h.root)
    a._agent_workspace_dir = str(h.root)
    a._permission_workspace_root = h.root
    a._sys_operation = h.sysop
    a._agent_name = "caller-code"
    spec, context = a._build_code_spec_snapshot(h.raw, h.raw["react"], h.model)
    await a.create_instance({"channel_id": "web", "project_dir": str(h.root)}, spec=spec, build_context=context)
    _verify(a)
    assert a._instance.deep_config.sys_operation is h.sysop
    await a.cleanup()
    assert Runner.resource_mgr.get_sys_operation(h.sysop.id) is h.sysop


async def test_registered_plan_guard_blocks_write_before_reviewer_with_real_sdk_state(code_cold, monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
    from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _Session
    h = code_cold
    await _create(h, sub_mode="plan")
    a = h.adapter
    session = _Session()
    state = a._instance.load_state(session)
    state.plan_mode.mode = "plan"
    a._instance.save_state(session, state)
    review = AsyncMock(side_effect=AssertionError("plan rejection reached reviewer"))
    monkeypatch.setattr(a._permission_rail, "_maybe_run_reviewer", review)
    ctx = AgentCallbackContext(agent=a._instance, session=session, extra={}, inputs=ToolCallInputs(
        tool_name="bash", tool_args={"command": "mkdir forbidden"},
        tool_call=SimpleNamespace(id="plan-block", name="bash", arguments={"command": "mkdir forbidden"})))
    assert a._instance.is_registered_rail(a._code_plan_pre_permission_rail)
    await a._code_plan_pre_permission_rail.before_tool_call(ctx)
    await a._permission_rail.before_tool_call(ctx)
    await a._code_agent_mode_rail.before_tool_call(ctx)
    assert plan_hard_rejected(ctx) and ctx.extra["_skip_tool"] is True
    review.assert_not_awaited()
    assert not (h.root / "forbidden").exists()


def _owned_probe_bundle(h, monkeypatch):
    h.raw["modes"]["code"]["tools"] = ["probe"]
    monkeypatch.setattr(h.adapter, "_get_tool_build_func", lambda *_args: [h.tool, h.shared_tool])
    def owned_operation():
        h.adapter._retain_sys_operation(h.sysop.id)
        return h.sysop
    monkeypatch.setattr(h.adapter, "_create_sys_operation", owned_operation)


async def _assert_cold_isolated(h):
    a = h.adapter
    assert a._permission_state.permission_isolated
    assert a._permission_state.permission_cleanup_complete
    assert a._permission_state.permission_epoch is None
    assert a.get_live_session_instance(a._parent_session_id) is None
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await a.ensure_instance()
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await _create(h)
    assert Runner.resource_mgr.get_tool(h.tool.card.id) is None
    assert Runner.resource_mgr.get_tool(h.shared_tool.card.id) is h.shared_tool
    if a._instance is not None:
        assert a._instance.configured_rails() == []
        assert h.callbacks(a._instance) == []


@pytest.mark.parametrize("failure", ["provider", "registration", "extension", "verify"])
@pytest.mark.parametrize("borrowed", [False, True])
async def test_smart_cold_failure_reclaims_actual_graph_and_provider_tools(code_cold, monkeypatch, failure, borrowed):
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec
    h = code_cold
    a = h.adapter
    _owned_probe_bundle(h, monkeypatch)
    kwargs = {}
    if borrowed:
        a._project_dir = str(h.root)
        a._agent_workspace_dir = str(h.root)
        a._permission_workspace_root = h.root
        a._sys_operation = h.sysop
        a._agent_name = "borrowed-cold"
        spec, context = a._build_code_spec_snapshot(h.raw, h.raw["react"], h.model)
        kwargs = {"spec": spec, "build_context": context}
    if failure == "provider":
        def fail_build(spec, context):
            spec.resolve_parts(context)
            assert Runner.resource_mgr.get_tool(h.tool.card.id) is h.tool
            assert a._instance is None
            raise RuntimeError("cold failure")
        monkeypatch.setattr(DeepAgentSpec, "build", fail_build)
    elif failure == "registration":
        register = DeepAgent._register_rail_selective
        async def fail_registration(instance, rail):
            await register(instance, rail)
            if isinstance(rail, AutoPermissionInterruptRail):
                raise RuntimeError("cold failure")
        monkeypatch.setattr(DeepAgent, "_register_rail_selective", fail_registration)
    elif failure == "extension":
        monkeypatch.setattr(a, "_load_active_packages", AsyncMock(side_effect=RuntimeError("cold failure")))
    else:
        monkeypatch.setattr(PermissionRailGroup, "verify", Mock(side_effect=RuntimeError("cold failure")))
    with pytest.raises(RuntimeError, match="cold failure"):
        await a.create_instance({"channel_id": "web", "project_dir": str(h.root)}, **kwargs)
    await _assert_cold_isolated(h)
    assert a._retained_sys_operation_ids == []
    if borrowed:
        assert Runner.resource_mgr.get_sys_operation(h.sysop.id) is h.sysop
    else:
        assert Runner.resource_mgr.get_sys_operation(h.sysop.id) is None


async def test_smart_cold_cancellation_settles_thread_before_cleanup(code_cold, monkeypatch):
    h = code_cold
    a = h.adapter
    _owned_probe_bundle(h, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    late = AgentRail()
    initialize = DeepAgent.ensure_initialized
    observed = []
    async def delayed_initialize(instance):
        await initialize(instance)
        observed.append(instance)
        entered.set()
        assert release.wait(10), "initializer barrier was not released"
        await instance.register_rail(late)
        finished.set()
    monkeypatch.setattr(DeepAgent, "ensure_initialized", delayed_initialize)
    isolate = AsyncMock(wraps=a._isolate_permission_instance)
    monkeypatch.setattr(a, "_isolate_permission_instance", isolate)
    task = asyncio.create_task(_create(h))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        for _ in range(20):
            await asyncio.sleep(0)
            if a._permission_state.permission_isolated:
                break
        assert a._permission_state.permission_isolated
        assert not task.done() and not finished.is_set()
        isolate.assert_not_awaited()
        assert a.get_live_session_instance(a._parent_session_id) is None
        with pytest.raises(RuntimeError, match="permission_session_isolated"):
            await a.ensure_instance()
        task.cancel()  # A second cancel must not abandon the still-running initializer.
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert not observed[0].is_registered_rail(late)
    await _assert_cold_isolated(h)


async def test_manual_cold_failure_keeps_existing_non_smart_contract(code_cold, monkeypatch):
    h = code_cold
    h.raw["permissions"]["mode"] = "manual"
    monkeypatch.setattr(h.adapter, "_load_active_packages", AsyncMock(side_effect=RuntimeError("manual failure")))
    with pytest.raises(RuntimeError, match="manual failure"):
        await _create(h)
    assert not h.adapter._permission_state.permission_isolated


@pytest.mark.parametrize("adapter_type", [interface_deep.JiuWenSwarmDeepAdapter, interface_code.JiuwenSwarmCodeAdapter])
async def test_waiting_instance_borrower_rechecks_isolation_after_lock(adapter_type):
    a = adapter_type()
    a._root_instance_lock = asyncio.Lock()
    await a._root_instance_lock.acquire()
    borrower = asyncio.create_task(a.ensure_instance())
    await asyncio.sleep(0)
    assert not borrower.done()
    a._instance = object()
    a._permission_state.begin_permission_isolation()
    a._root_instance_lock.release()
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await borrower
    assert a.get_live_session_instance("waiting") is None


async def test_code_owner_tracks_permission_group_lifecycle(code_cold):
    h = code_cold
    await _create(h)
    old = h.adapter._permission_rail
    assert old._owner_agent is h.adapter._instance
    h.change()
    await h.adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})
    assert old._owner_agent is None
    assert h.adapter._permission_rail._owner_agent is h.adapter._instance


async def test_control_fast_route_through_actual_inner_callback_dispatch(code_cold, monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, ToolCallInputs
    from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _Session
    h = code_cold
    await _create(h)
    agent, permission = h.adapter._instance, h.adapter._permission_rail
    reviewer = AsyncMock(side_effect=AssertionError("trusted Code control reached reviewer"))
    audit = Mock()
    monkeypatch.setattr(permission, "_maybe_run_reviewer", reviewer)
    monkeypatch.setattr(permission, "_emit_audit", audit)
    session = _Session()
    state = agent.load_state(session)
    state.plan_mode.mode = "plan"
    agent.save_state(session, state)
    args = {}
    from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
        bind_root_permission_request, reset_root_permission_request,
    )
    token = bind_root_permission_request(
        root_session_id=session.session_id, request_id="bridge-request", enabled=True,
        queue=h.adapter._root_permission_queue,
    )
    from openjiuwen.core.single_agent.rail.base import RunContext
    from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
        ROOT_CONTEXT_KEY, RootDecisionContext, RootIntentTurn, RootIntentTurnKind,
    )
    context = RootDecisionContext(session.session_id, "bridge-request", "web", (
        RootIntentTurn("bridge-request", RootIntentTurnKind.FRESH, "Plan a change"),
    ))
    inputs = ToolCallInputs(tool_name="enter_plan_mode", tool_args=args,
                           tool_call=SimpleNamespace(id="real-bridge", name="enter_plan_mode", arguments=args))
    inputs.run_context = RunContext(extra={ROOT_CONTEXT_KEY: context.to_mapping()})
    try:
        await agent.react_agent._execute_callbacks(
            AgentCallbackEvent.BEFORE_TOOL_CALL, inputs, session=session,
        )
    finally:
        reset_root_permission_request(token)
    reviewer.assert_not_awaited()
    assert any(call.kwargs.get("reason") == "code_plan_control" for call in audit.call_args_list), (audit.call_args_list, inputs.tool_result)


async def test_code_owner_clears_when_reinitialization_fails(code_cold, monkeypatch):
    h = code_cold
    await _create(h)
    permission = h.adapter._permission_rail
    monkeypatch.setattr(permission.base_rail, "init", Mock(side_effect=RuntimeError("init failed")), raising=False)
    with pytest.raises(RuntimeError, match="init failed"):
        permission.init(h.adapter._instance)
    assert permission._owner_agent is None
