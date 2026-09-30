"""Deep parent-owned required rail composition contracts."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
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
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_PROFILE_BUILDERS = """
_build_skill_rail _build_skill_retrieval_prompt_rail _build_symphony_orchestration_rail
_build_third_agent_prompt_rail
_build_runtime_prompt_rail _build_response_prompt_rail _build_multimodal_image_rail
_build_task_planning_rail _build_security_rail _build_heartbeat_rail
_build_model_anomaly_detection_rail _build_circuit_breaker_rail _build_avatar_rail
_build_memory_forbidden_rail _build_structured_ask_user_rail
_build_work_agent_mode_rail _build_work_plan_approval_rail
""".split()


def _adapter() -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
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
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        adapter,
        "_validate_required_agent_rails",
        lambda rails, **kwargs: captured.update(rails=list(rails), **kwargs),
    )
    adapter._build_agent_rails(
        {},
        {"permissions": {"enabled": mode != "disabled", "mode": mode}},
        mode="agent",
        composition_scope="single_agent",
    )
    return captured.pop("rails"), captured


@pytest.mark.parametrize("mode", ["disabled", "manual", "auto"])
def test_parent_composition_preserves_order_and_wiring(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    adapter = _adapter()
    rails, required = _capture(adapter, monkeypatch, mode)
    attrs = (
        "_root_permission_queue_rail _skill_rail _skill_retrieval_prompt_rail "
        "_symphony_orchestration_rail _third_agent_prompt_rail _root_context_rail "
        "_runtime_prompt_rail _response_prompt_rail _multimodal_image_rail "
        "_stream_event_rail _task_planning_rail _security_rail _heartbeat_rail "
        "_model_anomaly_detection_rail _circuit_breaker_rail _avatar_rail "
        "_memory_forbidden_rail _permission_rail "
        "_root_permission_completion_rail _context_processor_rail "
        "_ask_user_rail _work_agent_mode_rail _work_plan_approval_rail"
    ).split()
    expected_profile_rails = [
        getattr(adapter, attr)
        for attr in attrs
        if getattr(adapter, attr, None) is not None
    ]
    assert rails == expected_profile_rails
    queue_rail = required["queue_rail"]
    completion_rail = required["completion_rail"]
    execution = required["root_context_rail"]
    stream = required["stream_event_rail"]
    assert queue_rail.queue is adapter._root_permission_queue
    assert completion_rail.queue is adapter._root_permission_queue
    assert execution._root_permission_queue is adapter._root_permission_queue
    assert execution._command_sys_operation is adapter._sys_operation
    assert stream._root_permission_queue is adapter._root_permission_queue
    if mode != "disabled":
        permission = required["permission_rail"]
        ask = adapter._ask_user_rail
        plan = adapter._work_plan_approval_rail
        assert permission.priority >= ask.priority > plan.priority
        assert rails.index(permission) < rails.index(ask)


@pytest.mark.parametrize(
    ("case", "mode", "message"),
    [
        ("missing_required", "disabled", "required_agent_rail_count_invalid"),
        ("missing_completion", "disabled", "required_agent_rail_count_invalid"),
        ("missing_permission", "manual", "required_permission_rail_count_invalid"),
        ("duplicate_required", "disabled", "required_agent_rail_count_invalid"),
        ("duplicate_permission", "manual", "required_permission_rail_count_invalid"),
        ("wrong_manual", "manual", "required_permission_rail_type_invalid"),
        ("wrong_auto", "auto", "required_permission_rail_type_invalid"),
        ("graph_identity", "disabled", "required_agent_rail_graph_identity_mismatch"),
        ("adapter_identity", "disabled", "required_agent_rail_attr_identity_mismatch"),
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
    elif case.startswith("wrong_"):
        replacement = _permission(adapter, case == "wrong_manual")
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


@pytest.mark.parametrize(
    "reserved_attr", sorted(interface_deep._REQUIRED_AGENT_RAIL_ATTR_NAMES)
)
def test_reserved_required_attr_collision_precedes_profile_factories(
    monkeypatch: pytest.MonkeyPatch, reserved_attr: str
) -> None:
    adapter = _adapter()
    factory = MagicMock()
    specs = interface_deep._AgentProfileRailSpecs(
        before_execution_context=(
            interface_deep._AgentRailBuildSpec(reserved_attr, factory),
        )
    )
    monkeypatch.setattr(
        adapter, "_build_profile_rail_specs", lambda *args, **kwargs: specs
    )
    monkeypatch.setattr(interface_deep, "build_permission_rail", MagicMock())
    with pytest.raises(RuntimeError, match="profile_rail_reserved_attr_name"):
        adapter._build_agent_rails(
            {},
            {"permissions": {"enabled": False}},
            mode="agent",
            composition_scope="single_agent",
        )
    factory.assert_not_called()
    interface_deep.build_permission_rail.assert_not_called()


def test_parent_composition_requires_assigned_sys_operation() -> None:
    with pytest.raises(RuntimeError, match="required_agent_sys_operation_unavailable"):
        JiuWenSwarmDeepAdapter()._build_agent_rails(
            {},
            {"permissions": {"enabled": False}},
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
def test_excluded_scope_does_not_install_generic_permission_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    instance_mode: str,
    instance_sub_mode: str | None,
    composition_scope: str,
) -> None:
    adapter = _adapter()
    adapter._session_instance_mode = instance_mode
    adapter._session_instance_sub_mode = instance_sub_mode
    _stub_profile(adapter, monkeypatch)
    build_permission = MagicMock(
        side_effect=AssertionError("permission must not build")
    )
    monkeypatch.setattr(interface_deep, "build_permission_rail", build_permission)
    rails = adapter._build_agent_rails(
        {},
        {"permissions": {"enabled": True, "mode": "auto"}},
        mode=instance_mode,
        composition_scope=composition_scope,
    )

    assert adapter._enable_auto_permission is False
    assert adapter._permission_rail is None
    assert adapter._root_permission_queue_rail is None
    assert adapter._root_context_rail is None
    assert sum(isinstance(rail, JiuSwarmStreamEventRail) for rail in rails) == 1
    build_permission.assert_not_called()
