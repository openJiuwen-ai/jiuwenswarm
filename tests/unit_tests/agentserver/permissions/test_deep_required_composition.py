"""Deep parent-owned required rail composition contracts."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.observability import AgentObservabilityRail
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
    PlanApprovalInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
    StructuredAskUserRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionQueueRail,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, permission_rail_group
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

_PROFILE_BUILDERS = """
_build_skill_rail _build_skill_retrieval_prompt_rail _build_symphony_orchestration_rail
_build_runtime_prompt_rail _build_eternal_conversation_rail _build_response_prompt_rail
_build_multimodal_image_rail
_build_task_planning_rail _build_security_rail _build_heartbeat_rail
_build_model_anomaly_detection_rail _build_circuit_breaker_rail _build_avatar_rail
_build_memory_forbidden_rail _build_subagent_rail _build_structured_ask_user_rail
_build_work_agent_mode_rail _build_work_plan_approval_rail
""".split()


def _adapter(
    session_id: str = "permission-composition-session",
) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped(session_id)
    adapter._model = MagicMock()
    adapter._sys_operation = MagicMock()
    adapter._config_cache = {}
    adapter._config_base_cache = {}
    return adapter


def _permission(adapter: JiuWenSwarmDeepAdapter, auto: bool) -> object:
    rail = object.__new__(
        AutoPermissionInterruptRail if auto else PermissionInterruptRail
    )
    if auto:
        rail.sys_operation = adapter._sys_operation
        rail.priority = PermissionInterruptRail.priority
    return rail


def _stub_profile(
    adapter: JiuWenSwarmDeepAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_skill_include_tools_for_profile", lambda: ())
    for name in _PROFILE_BUILDERS:
        monkeypatch.setattr(adapter, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(
        adapter,
        "_build_structured_ask_user_rail",
        lambda: SimpleNamespace(priority=StructuredAskUserRail.priority),
    )
    monkeypatch.setattr(
        adapter,
        "_build_work_plan_approval_rail",
        lambda: SimpleNamespace(priority=PlanApprovalInterruptRail.priority),
    )
    monkeypatch.setattr(
        interface_deep, "_build_context_processor_rail", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        interface_deep, "load_hooks_config", lambda config: SimpleNamespace(events=())
    )


def _capture(
    adapter: JiuWenSwarmDeepAdapter,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[list[object], dict[str, object]]:
    _stub_profile(adapter, monkeypatch)
    permission = None if mode == "disabled" else _permission(adapter, mode == "auto")
    monkeypatch.setattr(
        interface_deep, "build_permission_rail", lambda **kwargs: permission
    )
    rails = adapter._build_agent_rails(
        {},
        {"permissions": {"enabled": mode != "disabled", "mode": mode}},
        mode="agent",
        composition_scope="single_agent",
    )
    return rails, {
        "queue_rail": adapter._root_permission_queue_rail,
        "completion_rail": adapter._root_permission_completion_rail,
        "root_context_rail": adapter._root_context_rail,
        "stream_event_rail": adapter._stream_event_rail,
        "permission_rail": adapter._permission_rail,
    }


@pytest.mark.parametrize("mode", ["disabled", "manual", "auto"])
def test_parent_composition_preserves_order_and_wiring(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    adapter = _adapter()
    rails, required = _capture(adapter, monkeypatch, mode)
    attrs = (
        "_root_permission_queue_rail _root_context_rail "
        "_runtime_prompt_rail _skill_rail _skill_retrieval_prompt_rail "
        "_symphony_orchestration_rail _response_prompt_rail "
        "_multimodal_image_rail "
        "_stream_event_rail _task_planning_rail _security_rail "
        "_model_anomaly_detection_rail _heartbeat_rail _circuit_breaker_rail _avatar_rail "
        "_memory_forbidden_rail _subagent_rail _permission_rail "
        "_root_permission_completion_rail _context_processor_rail _eternal_conversation_rail "
        "_ask_user_rail _work_agent_mode_rail _work_plan_approval_rail"
    ).split()
    expected_profile_rails = [
        getattr(adapter, attr)
        for attr in attrs
        if getattr(adapter, attr, None) is not None
    ]
    assert rails[:-1] == expected_profile_rails
    assert isinstance(rails[-1], AgentObservabilityRail)
    queue_rail = required["queue_rail"]
    completion_rail = required["completion_rail"]
    execution = required["root_context_rail"]
    stream = required["stream_event_rail"]
    if mode == "auto":
        assert queue_rail.queue is adapter._root_permission_queue
        assert completion_rail.queue is adapter._root_permission_queue
        assert execution._root_permission_queue is adapter._root_permission_queue
        assert execution._command_sys_operation is adapter._sys_operation
        assert stream._root_permission_queue is adapter._root_permission_queue
    else:
        assert queue_rail is completion_rail is execution is None
        assert stream._root_permission_queue is None
    if mode != "disabled":
        permission = required["permission_rail"]
        ask = adapter._ask_user_rail
        plan = adapter._work_plan_approval_rail
        assert permission.priority >= ask.priority > plan.priority
        assert rails.index(permission) < rails.index(ask)


@pytest.mark.parametrize(
    ("case", "mode", "message"),
    [
        ("missing_required", "auto", "required_agent_rail_count_invalid"),
        ("missing_completion", "auto", "required_agent_rail_count_invalid"),
        ("missing_permission", "auto", "required_permission_rail_count_invalid"),
        ("duplicate_required", "auto", "required_agent_rail_count_invalid"),
        ("duplicate_permission", "auto", "required_permission_rail_count_invalid"),
        ("wrong_auto", "auto", "required_permission_rail_type_invalid"),
        ("graph_identity", "auto", "required_agent_rail_graph_identity_mismatch"),
        ("adapter_identity", "auto", "required_agent_rail_attr_identity_mismatch"),
        ("auto_provider", "auto", "required_permission_rail_identity_mismatch"),
    ],
)
def test_required_validation_rejects_invalid_composition(
    monkeypatch: pytest.MonkeyPatch, case: str, mode: str, message: str
) -> None:
    adapter = _adapter()
    rails, required = _capture(adapter, monkeypatch, mode)
    queue_rail = required["queue_rail"]
    permission = required["permission_rail"]
    if case == "missing_required":
        rails.remove(required["stream_event_rail"])
    elif case == "missing_completion":
        rails.remove(required["completion_rail"])
    elif case == "missing_permission":
        rails.remove(permission)
    elif case == "duplicate_required":
        rails.append(RootPermissionQueueRail(adapter._root_permission_queue))
    elif case == "duplicate_permission":
        rails.append(_permission(adapter, False))
    elif case == "wrong_auto":
        replacement = _permission(adapter, False)
        rails[rails.index(permission)] = replacement
        adapter._permission_rail = required["permission_rail"] = replacement
    elif case in {"graph_identity", "adapter_identity"}:
        replacement = RootPermissionQueueRail(adapter._root_permission_queue)
        if case == "graph_identity":
            rails[rails.index(queue_rail)] = replacement
        else:
            adapter._root_permission_queue_rail = replacement
    else:
        permission.sys_operation = object()

    with pytest.raises(RuntimeError, match=message):
        JiuWenSwarmDeepAdapter._validate_required_agent_rails(
            adapter, rails, **required
        )


def test_real_build_rejects_duplicate_required_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    _stub_profile(adapter, monkeypatch)
    monkeypatch.setattr(
        adapter, "_build_skill_rail",
        lambda **kwargs: RootPermissionQueueRail(adapter._root_permission_queue),
    )
    monkeypatch.setattr(
        interface_deep, "build_permission_rail", lambda **kwargs: _permission(adapter, True)
    )
    with pytest.raises(RuntimeError, match="required_agent_rail_count_invalid"):
        adapter._build_agent_rails(
            {},
            {"permissions": {"enabled": True, "mode": "auto"}},
            mode="agent",
            composition_scope="single_agent",
        )


def test_parent_composition_requires_assigned_sys_operation() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("permission-composition-session")
    with pytest.raises(RuntimeError, match="required_agent_sys_operation_unavailable"):
        adapter._build_agent_rails(
            {},
            {"permissions": {"enabled": True, "mode": "auto"}},
            mode="agent",
            composition_scope="single_agent",
        )


@pytest.mark.parametrize(
    ("mode", "sub_mode", "expected"),
    [
        ("agent", None, "single_agent"),
        ("code", "normal", "single_agent"),
        ("team", "plan", "team_root"),
        ("code", "team", "team_root"),
        ("auto_harness", "auto_harness", "auto_harness"),
    ],
)
def test_composition_scope_resolver_accepts_only_host_profiles(
    mode: str,
    sub_mode: str | None,
    expected: str,
) -> None:
    assert interface_deep._resolve_agent_composition_scope(mode, sub_mode) == expected


@pytest.mark.parametrize(
    ("mode", "sub_mode"),
    [
        ("", None),
        ("unknown", None),
        ("unknown", "team"),
        ("agent", "unknown"),
        ("code", "unknown"),
        ("team", "unknown"),
        ("auto_harness", "unknown"),
    ],
)
def test_composition_scope_resolver_rejects_unclassified_profiles(
    mode: str,
    sub_mode: str | None,
) -> None:
    with pytest.raises(RuntimeError, match="agent_composition_scope_unclassified"):
        interface_deep._resolve_agent_composition_scope(mode, sub_mode)


@pytest.mark.parametrize(
    ("instance_mode", "instance_sub_mode", "composition_scope"),
    [
        ("team", None, "team_root"),
        ("code", "team", "team_member"),
        ("auto_harness", "auto_harness", "auto_harness"),
    ],
)
def test_excluded_scope_keeps_manual_factory_without_smart_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    instance_mode: str,
    instance_sub_mode: str | None,
    composition_scope: str,
) -> None:
    adapter = _adapter()
    adapter._session_instance_mode = instance_mode
    adapter._session_instance_sub_mode = instance_sub_mode
    _stub_profile(adapter, monkeypatch)
    permission = _permission(adapter, False)
    build_permission = MagicMock(return_value=permission)
    monkeypatch.setattr(interface_deep, "build_permission_rail", build_permission)
    rails = adapter._build_agent_rails(
        {},
        {"permissions": {"enabled": True, "mode": "auto"}},
        mode=instance_mode,
        composition_scope=composition_scope,
    )

    assert adapter._enable_auto_permission is False
    assert adapter._permission_rail is permission
    assert adapter._root_permission_queue_rail is None
    assert adapter._root_context_rail is None
    assert sum(isinstance(rail, JiuSwarmStreamEventRail) for rail in rails) == 1
    build_permission.assert_called_once()
    assert build_permission.call_args.kwargs["enable_auto_permission"] is False


@pytest.mark.parametrize("mode", ["disabled", "manual", "auto"])
def test_cold_build_passes_session_context_and_gates_strict_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mode: str,
) -> None:
    adapter = _adapter("permission-build-session")
    adapter._permission_workspace_root = tmp_path / "project"
    adapter._workspace_dir = str(tmp_path / "workspace")
    adapter._platform_trusted_root = tmp_path / "platform"
    adapter._permissions_changed_notifier = MagicMock()
    adapter._browser_runtime_security_profile = object()
    _stub_profile(adapter, monkeypatch)
    monkeypatch.setattr(
        adapter, "_build_structured_ask_user_rail",
        JiuWenSwarmDeepAdapter._build_structured_ask_user_rail.__get__(adapter),
    )
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    permission = None if mode == "disabled" else _permission(adapter, mode == "auto")
    build = MagicMock(return_value=permission)
    monkeypatch.setattr(interface_deep, "build_permission_rail", build)
    adapter._build_agent_rails(
        {}, {"permissions": {"enabled": mode != "disabled", "mode": mode}},
    )
    kwargs = build.call_args.kwargs
    assert kwargs["session_id"] == adapter._parent_session_id
    assert kwargs["llm"] is adapter._model
    assert kwargs["sys_operation"] is adapter._sys_operation
    assert kwargs["permissions_changed_notifier"] is adapter._permissions_changed_notifier
    assert kwargs["browser_runtime_security_profile"] is adapter._browser_runtime_security_profile
    assert kwargs["workspace_root"] == (
        adapter._permission_workspace_root if mode == "auto" else adapter._workspace_dir
    )
    assert kwargs["platform_trusted_root"] == (
        adapter._platform_trusted_root if mode == "auto" else None
    )
    assert kwargs["trusted_search_urls"] is (
        adapter._trusted_search_urls if mode == "auto" else None
    )
    assert adapter._ask_user_rail._strict_continuation_contract is (mode == "auto")


@pytest.mark.parametrize("missing_builder", ["permission", "stream"])
@pytest.mark.parametrize("smart", [False, True])
def test_required_factory_failure_blocks_only_smart_build(
    monkeypatch: pytest.MonkeyPatch, missing_builder: str, smart: bool,
) -> None:
    adapter = _adapter()
    _stub_profile(adapter, monkeypatch)
    monkeypatch.setattr(
        interface_deep, "build_permission_rail",
        lambda **kwargs: None if missing_builder == "permission" else _permission(adapter, smart),
    )
    if missing_builder == "stream":
        if smart:
            monkeypatch.setattr(permission_rail_group, "JiuSwarmStreamEventRail", lambda **kwargs: None)
        else:
            monkeypatch.setattr(adapter, "_build_stream_event_rail", lambda: None)
    config = {"permissions": {"enabled": True, "mode": "auto" if smart else "manual"}}
    if smart:
        with pytest.raises(RuntimeError, match="required_agent_rail_missing"):
            adapter._build_agent_rails({}, config)
    else:
        adapter._build_agent_rails({}, config)


def test_code_profile_cannot_activate_smart_permission() -> None:
    adapter = JiuwenSwarmCodeAdapter()
    adapter.mark_as_session_scoped("permission-code-session")
    assert adapter._auto_permission_enabled_for_config(
        {"enabled": True, "mode": "auto"}, composition_scope="single_agent",
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
async def test_built_permission_group_registers_once_and_cleans_up_in_real_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path, smart: bool,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    adapter = _adapter()
    adapter._model = None  # Registration does not call a model or create a reviewer.
    adapter._permission_workspace_root = tmp_path
    _stub_profile(adapter, monkeypatch)
    for name in _PROFILE_BUILDERS:
        monkeypatch.setattr(adapter, name, lambda **kwargs: None)
    monkeypatch.setattr(interface_deep, "_build_context_processor_rail", lambda **kwargs: None)
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {})
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda session_id: {})
    rails = adapter._build_agent_rails(
        {}, {"permissions": {"enabled": True, "mode": "auto" if smart else "manual"}},
    )
    agent = DeepAgent(AgentCard(name="permission-composition-contract"))
    try:
        agent.configure(DeepAgentConfig(rails=rails, auto_create_workspace=False))
        for _ in range(2):
            await agent.ensure_initialized()
            for rail in rails:
                assert agent.is_registered_rail(rail)
                assert agent.find_rails_by_type((type(rail),)) == [rail]
            assert agent.find_rails_by_type(adapter._permission_rail_types()) == [
                adapter._permission_rail,
            ]
        for rail in rails:
            await agent.unregister_rail(rail)
            assert not agent.is_registered_rail(rail)
            assert not agent.find_rails_by_type((type(rail),))
    finally:
        await agent._agent_callback_manager.clear()
        if agent._react_agent is not None:
            await agent._react_agent.agent_callback_manager.clear()
