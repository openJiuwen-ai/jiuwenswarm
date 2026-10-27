# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OfficeClaw 默认与显式 Flash 的路由、存量会话及恢复契约。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import _default, _shared
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session import session_metadata


def request(params, *, method=ReqMethod.CHAT_SEND, channel="officeclaw"):
    return AgentRequest(
        request_id="explicit-flash-test",
        channel_id=channel,
        session_id="explicit-flash-session",
        req_method=method,
        params=dict(params),
    )


@pytest.fixture
def global_flash(monkeypatch):
    config = {"flash": {"enabled": True}}
    monkeypatch.setattr(_shared, "get_config", lambda: config)
    return config


@pytest.mark.parametrize("method", [
    ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME, ReqMethod.CHAT_ANSWER,
    ReqMethod.SESSION_CREATE,
])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("params,expected", [
    ({}, "agent"),
    ({"mode": "agent"}, "agent"),
    ({"mode": "agent.plan"}, "agent"),
    ({"mode": "agent", "work_mode": "work"}, "agent"),
    ({"mode": "agent.work.normal", "work_mode": "work"}, "agent"),
    ({"mode": "flash"}, "flash"),
])
def test_foreground_mode_uses_config_unless_flash_is_explicit(
    global_flash, method, enabled, params, expected,
):
    global_flash["flash"]["enabled"] = enabled
    if enabled:
        expected = "flash"
    resolved = _shared.resolve_request_runtime_mode(request(params, method=method))
    assert resolved.manager_mode == expected
    assert resolved.adapter_mode == expected
    if params.get("mode") != "flash":
        assert resolved.canonical_mode in {"agent", "agent.work.normal"}
        assert resolved.normal_mode == resolved.canonical_mode


@pytest.mark.parametrize("channel", ["web", "tui", "__cron__"])
def test_other_channels_keep_implicit_flash(global_flash, channel):
    resolved = _shared.resolve_request_runtime_mode(
        request({"mode": "agent"}, channel=channel),
    )
    assert resolved.manager_mode == "flash"
    assert resolved.canonical_mode == "agent"


@pytest.mark.parametrize("source", ["cron", "heartbeat", "proactive"])
def test_background_officeclaw_keeps_existing_strategy(global_flash, source):
    incoming = request({"mode": "agent"})
    incoming.metadata = {"source": source}
    assert _shared.resolve_request_runtime_mode(incoming).manager_mode == "flash"


@pytest.mark.parametrize("mode", ["flash.fast", "flash.normal", "flash.unknown"])
def test_unimplemented_flash_submode_is_rejected(global_flash, mode):
    with pytest.raises(ValueError, match="unsupported OfficeClaw mode"):
        _shared.resolve_request_runtime_mode(request({"mode": mode}))


def test_explicit_flash_does_not_read_global_switch(monkeypatch):
    config = Mock(side_effect=AssertionError("global switch must not be read"))
    monkeypatch.setattr(_shared, "get_config", config)
    resolved = _shared.resolve_request_runtime_mode(request({"mode": "flash"}))
    assert resolved.canonical_mode == "flash"
    assert resolved.manager_mode == resolved.adapter_mode == "flash"
    config.assert_not_called()


def test_active_plan_wins_over_relay_default_flash(global_flash):
    incoming = request({"mode": "flash", "work_mode": "work"})
    _shared._plan_active_sessions.add(incoming.session_id)
    try:
        resolved = _shared.resolve_request_runtime_mode(incoming)
        assert (resolved.manager_mode, resolved.sub_mode) == ("agent", "plan")
        assert resolved.is_plan
        assert resolved.adapter_mode == "agent"
    finally:
        _shared._plan_active_sessions.discard(incoming.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_mode,adapter,incoming_params,expected_mode,expected_sub,expected_business", [
    ("flash", "flash", {"query": "continue"}, "flash", None, "flash"),
    ("flash", "flash", {
        "source": "permission_interrupt", "answers": [],
        "request_id": "original-request",
    }, "flash", None, "flash"),
    ("code.normal", "code", {
        "source": "permission_interrupt", "answers": [],
        "request_id": "child-request",
    }, "code", "normal", "code.normal"),
    ("agent", "flash", {"mode": "agent.plan", "query": "continue"}, "flash", None, "agent"),
    ("agent.work.plan", "agent", {"mode": "flash", "query": "continue"}, "agent", "plan", "agent.work.plan"),
    ("agent", "agent", {"mode": "agent.plan", "query": "continue"}, "flash", None, "agent"),
    ("agent", "flash", {
        "source": "permission_interrupt", "answers": [],
        "request_id": "old-flash-request",
    }, "flash", None, "flash"),
    ("agent.work.normal", "flash", {
        "mode": "flash", "source": "permission_interrupt", "answers": [],
        "request_id": "old-flash-explicit-request",
    }, "flash", None, "flash"),
    ("agent.work.plan", "flash", {
        "mode": "flash", "query": "continue plan",
    }, "agent", "plan", "agent.work.plan"),
    ("agent", "agent", {
        "mode": "flash", "source": "permission_interrupt", "answers": [],
        "request_id": "old-deep-request",
    }, "agent", None, "agent"),
    ("code.normal", "code", {
        "mode": "flash", "source": "permission_interrupt", "answers": [],
        "request_id": "hosted-child-request",
    }, "code", "normal", "code.normal"),
])
async def test_prepare_and_tenant_lookup_keep_same_owner_mode(
    global_flash, monkeypatch, tmp_path, stored_mode, adapter,
    incoming_params, expected_mode, expected_sub, expected_business,
):
    incoming = request(incoming_params)
    metadata = {
        "mode": stored_mode, "adapter_mode": adapter, "work_mode": "work",
    }
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **kw: metadata)
    sync = Mock(return_value=None)
    monkeypatch.setattr(_default, "_sync_chat_request_metadata", sync)
    selected = object()
    manager = SimpleNamespace(
        wait_for_session_prewarm=AsyncMock(),
        get_agent=AsyncMock(return_value=selected),
    )
    ctx = SimpleNamespace(services=SimpleNamespace(agent_manager=manager))

    mode, sub, agent = await _default._prepare_code_mode_chat_turn(ctx, incoming, "officeclaw")
    assert (mode, sub, agent) == (expected_mode, expected_sub, selected)
    assert manager.get_agent.await_args.kwargs["mode"] == expected_mode

    # 真实租户链会二次解析；旧 Flash 亲和与恢复出的 Plan 都必须保持。
    tenant = AgentManager(agent_id="officeace", service_id="officeace", workspace_key="officeace")
    lookup_mode, lookup_sub, _ = tenant._agent_lookup_from_request(incoming)
    assert (lookup_mode, lookup_sub) == (expected_mode, expected_sub)
    assert incoming.resolved_adapter_mode == expected_mode
    assert incoming.params["mode"] == expected_business


def test_manager_restores_mode_for_officeclaw_followup(global_flash, monkeypatch):
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **kw: {
        "mode": "flash", "adapter_mode": "flash", "work_mode": "work",
    })
    manager = AgentManager(agent_id="officeace", service_id="officeace", workspace_key="officeace")
    incoming = request({"query": "continue"})
    assert manager._agent_lookup_from_request(incoming)[:2] == ("flash", None)
    assert incoming.params["mode"] == "flash"


def test_stale_flash_affinity_does_not_override_code_work_mode(global_flash, monkeypatch):
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **kw: {
        "mode": "agent", "adapter_mode": "flash", "work_mode": "code",
    })
    manager = AgentManager(agent_id="officeace", service_id="officeace", workspace_key="officeace")
    incoming = request({
        "mode": "flash", "work_mode": "code", "source": "permission_interrupt",
        "answers": [], "request_id": "code-request",
    })
    assert manager._agent_lookup_from_request(incoming)[:2] == ("code", "normal")
    assert incoming.resolved_adapter_mode == "code"
