# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron permission-rail policy differs by edition."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    resolve_cron_execution_flag,
)


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


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("a4p_enabled", [False, True])
def test_enterprise_cron_keeps_permission_rail_independent_of_a4p(
    monkeypatch, mode, a4p_enabled,
):
    """Enterprise cron mounts the guardrail regardless of A4P."""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.is_enterprise", lambda: True,
    )
    permission_rail = object()
    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = "__cron__"
    adapter._is_cron_execution = True
    adapter = adapter._new_session_scoped_adapter("scheduled-without-cron-prefix")
    _stub_deep_rail_builders(adapter, monkeypatch, permission_rail=permission_rail)

    config = {
        "models": {},
        "permissions": {"enabled": True, "mode": mode},
        "a4p": {"enabled": a4p_enabled},
    }
    rails = adapter._build_agent_rails({}, config, mode="agent")

    assert permission_rail in rails
    assert adapter._skip_cron_permission_rail(config) is False


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_personal_cron_skips_permission_rail_without_a4p(monkeypatch, mode):
    """Personal cron keeps the unattended default: no rail unless A4P is on."""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.is_enterprise", lambda: False,
    )
    permission_rail = object()
    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = "__cron__"
    adapter._is_cron_execution = True
    adapter = adapter._new_session_scoped_adapter("scheduled-without-cron-prefix")
    _stub_deep_rail_builders(adapter, monkeypatch, permission_rail=permission_rail)

    config = {
        "models": {},
        "permissions": {"enabled": True, "mode": mode},
        "a4p": {"enabled": False},
    }
    rails = adapter._build_agent_rails({}, config, mode="agent")

    assert permission_rail not in rails
    assert adapter._skip_cron_permission_rail(config) is True


def test_deep_adapter_keeps_permission_interrupt_rail_for_user_session(monkeypatch):
    permission_rail = object()
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("cron_prefix_but_user_session")
    adapter._is_cron_execution = False
    _stub_deep_rail_builders(adapter, monkeypatch, permission_rail=permission_rail)

    rails = adapter._build_agent_rails({}, {"models": {}}, mode="agent")

    assert permission_rail in rails
    assert adapter._auto_permission_capability_enabled()


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_enterprise_update_permission_rail_creates_rail_for_cron_session(monkeypatch, mode):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.is_enterprise", lambda: True,
    )
    created = []

    def fake_build_permission_rail(**_kwargs):
        rail = SimpleNamespace(name="permission")
        created.append(rail)
        return rail

    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = "__cron__"
    adapter._is_cron_execution = True
    adapter = adapter._new_session_scoped_adapter("scheduled-without-cron-prefix")
    adapter._permission_rail = None
    adapter._model = None
    monkeypatch.setattr(
        interface_deep_module, "build_permission_rail", fake_build_permission_rail
    )

    adapter._update_permission_rail({"permissions": {"enabled": True, "mode": mode}, "models": {}})

    assert adapter._permission_rail is not None
    assert created == [adapter._permission_rail]


@pytest.mark.parametrize(
    ("channel_id", "is_session_scoped", "config", "expected"),
    [
        ("__cron__", False, None, True),
        # The root Web adapter is only a router and must never become cron.
        ("web", False, {"cron_execution": True}, False),
    ],
)
def test_resolve_cron_execution_flag(channel_id, is_session_scoped, config, expected):
    assert resolve_cron_execution_flag(
        channel_id=channel_id,
        is_session_scoped=is_session_scoped,
        config=config,
    ) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("cron_execution", "enterprise", "skip_expected"), [
    (True, True, False),
    (True, False, True),
    (False, False, False),
])
async def test_code_adapter_cron_skip_depends_on_edition(
    monkeypatch, cron_execution, enterprise, skip_expected,
):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.is_enterprise", lambda: enterprise,
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter.mark_as_session_scoped("sess_web")
    adapter._channel_id = "web"
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: True)

    await adapter._create_instance(
        {"channel_id": "web", "cron_execution": cron_execution}, mode="code",
    )

    assert adapter._is_cron_execution is cron_execution
    assert adapter._skip_cron_permission_rail({}) is skip_expected
