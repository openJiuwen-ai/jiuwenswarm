# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron execution sessions must not mount PermissionInterruptRail (option A)."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class FakeHooksConfig:
    events = {}


def _stub_deep_rail_builders(adapter, monkeypatch, *, permission_rail):
    """Replace rail builders so _build_agent_rails only needs permission gating."""
    noop = lambda **_kwargs: None  # noqa: E731
    for name in (
        "_build_runtime_prompt_rail",
        "_build_response_prompt_rail",
        "_build_stream_event_rail",
        "_build_task_planning_rail",
        "_build_security_rail",
        "_build_heartbeat_rail",
        "_build_circuit_breaker_rail",
        "_build_avatar_rail",
        "_build_memory_forbidden_rail",
        "_build_subagent_rail",
        "_build_skill_retrieval_prompt_rail",
        "_build_structured_ask_user_rail",
        "_build_work_agent_mode_rail",
        "_build_work_plan_approval_rail",
    ):
        monkeypatch.setattr(adapter, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_build_multimodal_image_rail", noop)
    monkeypatch.setattr(adapter, "_build_model_anomaly_detection_rail", noop)
    monkeypatch.setattr(adapter, "_build_skill_rail", noop)
    monkeypatch.setattr(adapter, "_build_symphony_orchestration_rail", noop)
    monkeypatch.setattr(
        interface_deep_module,
        "build_permission_rail",
        lambda **_kwargs: permission_rail,
    )
    monkeypatch.setattr(
        interface_deep_module, "_build_context_processor_rail", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        interface_deep_module, "load_hooks_config", lambda _config: FakeHooksConfig()
    )


def test_deep_adapter_omits_permission_interrupt_rail_for_cron_session(monkeypatch):
    permission_rail = object()
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("cron_19abc_job1")
    _stub_deep_rail_builders(adapter, monkeypatch, permission_rail=permission_rail)

    rails = adapter._build_agent_rails({}, {"models": {}}, mode="agent")

    assert permission_rail not in rails


def test_deep_adapter_keeps_permission_interrupt_rail_for_user_session(monkeypatch):
    permission_rail = object()
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("sess_19abc")
    _stub_deep_rail_builders(adapter, monkeypatch, permission_rail=permission_rail)

    rails = adapter._build_agent_rails({}, {"models": {}}, mode="agent")

    assert permission_rail in rails


def test_update_permission_rail_does_not_create_rail_for_cron_session(monkeypatch):
    created = []

    def fake_build_permission_rail(**_kwargs):
        rail = SimpleNamespace(name="permission")
        created.append(rail)
        return rail

    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("cron_19abc_job1")
    adapter._permission_rail = None
    adapter._model = None
    monkeypatch.setattr(
        interface_deep_module, "build_permission_rail", fake_build_permission_rail
    )

    adapter._update_permission_rail({"permissions": {"enabled": True}, "models": {}})

    assert adapter._permission_rail is None
    assert created == []
