# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash configuration must survive Web mode composition and continuation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import _default, _shared
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


@pytest.fixture
def flash_config(monkeypatch):
    config = {"flash": {"enabled": True, "skill_selection": {"enabled": True}}}
    monkeypatch.setattr(_shared, "get_config", lambda: config)
    return config


def make_request(params, *, channel="web", request_id="flash-routing-1"):
    return AgentRequest(
        request_id=request_id,
        channel_id=channel,
        session_id="flash-routing-session",
        req_method=ReqMethod.CHAT_SEND,
        params=dict(params),
    )


@pytest.mark.parametrize("mode", ["agent", "agent.work.normal"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("skill_selection", [False, True])
def test_work_mode_obeys_flash_switch(
    flash_config, mode, enabled, skill_selection,
):
    flash_config["flash"]["enabled"] = enabled
    flash_config["flash"]["skill_selection"]["enabled"] = skill_selection
    request = make_request({"mode": mode, "work_mode": "work"})

    resolved = _shared.resolve_request_runtime_mode(request)

    expected_mode = "flash" if enabled else mode
    assert resolved.canonical_mode == expected_mode
    assert resolved.manager_mode == ("flash" if enabled else "agent")
    assert resolved.normal_mode == expected_mode
    assert resolved.sub_mode is None
    assert resolved.work_mode == "work"
    assert not resolved.is_plan
    assert not resolved.is_team
    assert not resolved.is_code_profile


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "agent", "work_mode": "code"},
        {"mode": "agent.plan", "work_mode": "work"},
        {"mode": "agent.plan", "work_mode": "code"},
        {"mode": "agent.work.plan", "work_mode": "work"},
        {"mode": "agent.code.normal", "work_mode": "work"},
        {"mode": "agent.code.plan", "work_mode": "work"},
        {"mode": "code.normal"},
        {"mode": "code.plan"},
        {"mode": "team", "work_mode": "work"},
        {"mode": "team", "work_mode": "code"},
        {"mode": "team.plan.normal", "work_mode": "work"},
        {"mode": "team.work.plan", "work_mode": "work"},
        {"mode": "team.code.normal", "work_mode": "code"},
    ],
)
def test_flash_switch_preserves_code_team_and_plan(flash_config, params):
    request = make_request(params)
    flash_config["flash"]["enabled"] = False
    baseline = _shared.resolve_request_runtime_mode(request)
    flash_config["flash"]["enabled"] = True

    assert _shared.resolve_request_runtime_mode(request) == baseline
    assert baseline.manager_mode != "flash"


def test_session_work_mode_override_keeps_code_profile(flash_config):
    request = make_request({"mode": "agent", "work_mode": "work"})

    resolved = _shared.resolve_request_runtime_mode(request, work_mode="code")

    assert resolved.manager_mode == "code"
    assert resolved.canonical_mode == "code.normal"
    assert resolved.is_code_profile


@pytest.mark.parametrize("channel", ["web", "tui", "officeclaw", "__cron__"])
def test_explicit_plan_entry_keeps_plan_when_flash_enabled(flash_config, channel):
    request = make_request(
        {"mode": "agent.plan", "work_mode": "work", "plan_entry_source": "e2a"},
        channel=channel,
    )

    resolved = _shared.resolve_request_runtime_mode(request)

    assert resolved.manager_mode == "agent"
    assert resolved.sub_mode == "plan"
    assert resolved.is_plan


def test_legacy_officeclaw_plan_token_obeys_flash_config(flash_config):
    request = make_request(
        {"mode": "agent.plan", "work_mode": "work"}, channel="officeclaw",
    )

    resolved = _shared.resolve_request_runtime_mode(request)

    assert resolved.canonical_mode == "flash"
    assert not resolved.is_plan


@pytest.mark.parametrize("config", [None, {}, {"flash": {"enabled": False}}])
def test_missing_or_disabled_flash_keeps_normal_work(monkeypatch, config):
    monkeypatch.setattr(_shared, "get_config", lambda: config)
    request = make_request({"mode": "agent", "work_mode": "work"})

    assert _shared.resolve_request_runtime_mode(request).canonical_mode == "agent"


def test_config_read_failure_keeps_normal_work(monkeypatch):
    monkeypatch.setattr(_shared, "get_config", Mock(side_effect=OSError("unavailable")))
    request = make_request({"mode": "agent", "work_mode": "work"})

    assert _shared.resolve_request_runtime_mode(request).canonical_mode == "agent"


def test_config_changes_apply_to_next_request(flash_config):
    for enabled, expected in [(False, "agent"), (True, "flash"), (False, "agent")]:
        flash_config["flash"]["enabled"] = enabled
        request = make_request({"mode": "agent", "work_mode": "work"})
        assert _shared.resolve_request_runtime_mode(request).canonical_mode == expected


def test_explicit_flash_works_with_default_switch_off(flash_config):
    flash_config["flash"]["enabled"] = False
    request = make_request({"mode": "flash", "work_mode": "work"})

    assert _shared.resolve_request_runtime_mode(request).canonical_mode == "flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_first_and_continuation_select_configured_adapter(
    flash_config, monkeypatch, tmp_path, enabled,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    flash_config["flash"]["enabled"] = enabled
    expected = "flash" if enabled else "agent"
    metadata = {"work_mode": "work", "mode": "agent"}
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **kw: metadata)
    sync_metadata = Mock(return_value=None)
    monkeypatch.setattr(_default, "_sync_chat_request_metadata", sync_metadata)
    agent = object()
    manager = SimpleNamespace(
        wait_for_session_prewarm=AsyncMock(),
        get_agent=AsyncMock(return_value=agent),
    )
    ctx = SimpleNamespace(services=SimpleNamespace(agent_manager=manager))

    for turn in range(2):
        request = make_request(
            {"mode": "agent", "work_mode": "work", "query": "hello"},
            request_id=f"flash-routing-{turn}",
        )
        mode, sub_mode, selected = await _default._prepare_code_mode_chat_turn(
            ctx, request, "web",
        )

        assert (mode, sub_mode, selected) == (expected, None, agent)
        assert request.params["mode"] == expected
        assert JiuWenSwarm._adapter_mode_for_request(request) == expected
        manager.get_agent.assert_awaited_with(
            channel_id="web", mode=expected, project_dir=None, sub_mode=None,
        )
        assert sync_metadata.call_args.args[2] == expected
        metadata["mode"] = expected
