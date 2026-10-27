# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash configuration must survive Web mode composition and continuation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import create_adapter
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import _default, _shared
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.agent_adapter import interface as adapter_interface
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

    expected_adapter_mode = "flash" if enabled else "agent"
    assert resolved.canonical_mode == mode
    assert resolved.manager_mode == ("flash" if enabled else "agent")
    assert resolved.normal_mode == mode
    assert resolved.adapter_mode == expected_adapter_mode
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


def test_legacy_officeclaw_plan_token_uses_flash_config_without_entering_plan(flash_config):
    request = make_request(
        {"mode": "agent.plan", "work_mode": "work"}, channel="officeclaw",
    )

    resolved = _shared.resolve_request_runtime_mode(request)

    assert resolved.canonical_mode == "agent"
    assert resolved.manager_mode == "flash"
    assert resolved.adapter_mode == "flash"
    assert resolved.normal_mode == "agent"
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
        resolved = _shared.resolve_request_runtime_mode(request)
        assert resolved.canonical_mode == "agent"
        assert resolved.adapter_mode == expected


def test_explicit_flash_works_with_default_switch_off(flash_config):
    flash_config["flash"]["enabled"] = False
    request = make_request({"mode": "flash", "work_mode": "work"})

    resolved = _shared.resolve_request_runtime_mode(request)
    assert resolved.canonical_mode == "flash"
    assert resolved.manager_mode == "flash"
    assert resolved.adapter_mode == "flash"


@pytest.mark.parametrize(
    "raw_mode",
    ["", "agent", "agent.fast", "fast", "plan"],
)
def test_legacy_agent_flash_injection_keeps_business_canonical(
    flash_config, raw_mode,
):
    assert _shared.resolve_agent_request_mode(raw_mode) == ("flash", None, "agent")


def test_flash_factory_creates_flash_adapter():
    adapter = create_adapter("harness", mode="flash")

    assert isinstance(adapter, JiuwenSwarmFlashAdapter)


def test_agent_manager_preserves_code_work_mode_when_flash_enabled(flash_config):
    manager = AgentManager(
        agent_id="officeace",
        service_id="officeace",
        workspace_key="officeace",
    )
    request = make_request(
        {"mode": "agent", "work_mode": "code"},
        channel="officeclaw",
    )

    assert manager._agent_lookup_from_request(request) == ("code", "normal", None)
    assert request.resolved_adapter_mode == "code"


@pytest.mark.asyncio
async def test_officeace_request_reaches_flash_factory_and_reuses_facade(
    flash_config, monkeypatch,
):
    manager = AgentManager(
        agent_id="officeace",
        service_id="officeace",
        workspace_key="officeace",
    )
    monkeypatch.setattr(
        manager,
        "_get_or_create_skill_manager",
        AsyncMock(return_value=None),
    )
    create_instance = AsyncMock()
    monkeypatch.setattr(JiuWenSwarm, "create_instance", create_instance)
    request = make_request(
        {"mode": "flash", "work_mode": "work", "query": "hello"},
        channel="officeclaw",
    )

    facade = await manager._get_agent_for_request(request)

    assert isinstance(facade, JiuWenSwarm)
    assert request.resolved_adapter_mode == "flash"
    assert facade._jiuwenswarm_agent_mode == "flash"
    assert create_instance.await_args.kwargs["mode"] == "flash"

    adapter = facade.ensure_adapter(mode="flash")
    assert isinstance(adapter, JiuwenSwarmFlashAdapter)
    assert facade._adapter_mode == "flash"

    second_request = make_request(
        {"mode": "flash", "work_mode": "work", "query": "continue"},
        channel="officeclaw",
        request_id="flash-routing-second",
    )
    second_facade = await manager._get_agent_for_request(second_request)

    assert second_facade is facade
    assert create_instance.await_count == 1
    assert facade.ensure_adapter(mode="flash") is adapter


@pytest.mark.asyncio
async def test_flash_enabled_interrupt_answer_returns_to_existing_deep_owner(
    flash_config,
):
    def _deep_owner(session_id: str):
        adapter = Mock()
        adapter._is_session_scoped_adapter = False
        adapter._sessions_awaiting_tool_permission = {session_id}
        adapter._session_adapter_last_used = {session_id: 10.0}
        adapter._session_adapters = {}
        adapter._instance = SimpleNamespace(
            _interaction_session=SimpleNamespace(session_id=session_id),
            _loop_session=None,
        )
        adapter._read_session_interrupt_state = Mock(
            return_value={"paused": True}
        )
        owner = Mock()
        owner._adapter = adapter
        return owner

    deep_owner = _deep_owner("deep-interrupt-session")
    flash_agent = Mock(name="new_flash_agent")
    manager = AgentManager(
        agent_id="officeace",
        service_id="officeace",
        workspace_key="officeace",
    )
    manager.agents = {
        "officeclaw": {
            "agent::": deep_owner,
            "flash::": flash_agent,
        },
    }
    manager.get_agent = AsyncMock()
    request = make_request(
        {
            "mode": "agent",
            "work_mode": "work",
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "deep-permission-1",
        },
        channel="officeclaw",
        request_id="deep-interrupt-answer",
    )
    request.session_id = "deep-interrupt-session"

    selected = await manager._get_agent_for_request(request)

    assert selected is deep_owner
    assert request.resolved_adapter_mode == "agent"
    manager.get_agent.assert_not_awaited()


def test_facade_rejects_cross_implementation_reuse(monkeypatch):
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = None
    facade._adapter_mode = None
    facade._workspace_dir = "workspace"
    facade._agent_id = None
    facade._service_id = None
    facade._skill_manager = Mock()
    facade._personal_context_runtime_enabled = False
    monkeypatch.setattr(adapter_interface, "resolve_sdk_choice", lambda: "harness")
    created = []

    def fake_create_adapter(_sdk, *, mode, **_kwargs):
        adapter = SimpleNamespace(mode=mode)
        created.append((mode, adapter))
        return adapter

    monkeypatch.setattr(adapter_interface, "create_adapter", fake_create_adapter)

    first = facade._ensure_adapter(mode="agent")
    assert facade._adapter_mode == "agent"
    assert facade._ensure_adapter() is first
    with pytest.raises(RuntimeError, match="adapter mode mismatch"):
        facade._ensure_adapter(mode="flash")
    assert created == [("agent", first)]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_first_and_continuation_select_configured_adapter(
    flash_config, monkeypatch, tmp_path, enabled,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    flash_config["flash"]["enabled"] = enabled
    expected_manager_mode = "flash" if enabled else "agent"
    expected_canonical_mode = "agent"
    expected_adapter_mode = expected_manager_mode
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

        assert (mode, sub_mode, selected) == (expected_manager_mode, None, agent)
        assert request.params["mode"] == expected_canonical_mode
        assert request.resolved_adapter_mode == expected_adapter_mode
        assert JiuWenSwarm._adapter_mode_for_request(request) == expected_adapter_mode
        manager.get_agent.assert_awaited_with(
            channel_id="web", mode=expected_manager_mode, project_dir=None, sub_mode=None,
        )
        assert sync_metadata.call_args.args[2] == expected_canonical_mode
        metadata["mode"] = expected_canonical_mode


@pytest.mark.asyncio
async def test_existing_office_session_keeps_flash_affinity_after_switch_off(
    flash_config, monkeypatch, tmp_path,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    session_id = "flash-sticky-session"
    session_metadata.init_session_metadata(
        session_id=session_id,
        channel_id="web",
        mode="agent",
        work_mode="work",
        adapter_mode="agent",
        sessions_root=tmp_path,
    )
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda _: tmp_path)
    agent = object()
    manager = SimpleNamespace(
        wait_for_session_prewarm=AsyncMock(),
        get_agent=AsyncMock(return_value=agent),
    )
    ctx = SimpleNamespace(services=SimpleNamespace(agent_manager=manager))

    # First turn upgrades the normal-office session from Deep to Flash while
    # the global switch is enabled.
    flash_config["flash"]["enabled"] = True
    request = make_request(
        {"mode": "agent", "work_mode": "work", "query": "hello"},
        request_id="flash-sticky-first",
    )
    request.session_id = session_id
    mode, sub_mode, selected = await _default._prepare_code_mode_chat_turn(
        ctx, request, "web",
    )

    assert (mode, sub_mode, selected) == ("flash", None, agent)
    assert request.params["mode"] == "agent"
    assert request.resolved_adapter_mode == "flash"
    manager.get_agent.assert_awaited_with(
        channel_id="web", mode="flash", project_dir=None, sub_mode=None,
    )
    persisted = session_metadata.get_session_metadata(
        session_id,
        cache_bust=True,
        enable_writeback=False,
        sessions_root=tmp_path,
    )
    assert persisted["mode"] == "agent"
    assert persisted["adapter_mode"] == "flash"

    # Turning the switch off only affects new sessions.  This already-bound
    # Flash conversation keeps its implementation and business canonical mode.
    flash_config["flash"]["enabled"] = False
    continuation = make_request(
        {"mode": "agent", "work_mode": "work", "query": "continue"},
        request_id="flash-sticky-second",
    )
    continuation.session_id = session_id
    mode, sub_mode, selected = await _default._prepare_code_mode_chat_turn(
        ctx, continuation, "web",
    )

    assert (mode, sub_mode, selected) == ("flash", None, agent)
    assert continuation.params["mode"] == "agent"
    assert continuation.resolved_adapter_mode == "flash"
    assert continuation.resolved_adapter_source == "session_sticky"
    assert manager.get_agent.await_count == 2
    assert manager.get_agent.await_args.kwargs["mode"] == "flash"
    persisted = session_metadata.get_session_metadata(
        session_id,
        cache_bust=True,
        enable_writeback=False,
        sessions_root=tmp_path,
    )
    assert persisted["mode"] == "agent"
    assert persisted["adapter_mode"] == "flash"


@pytest.mark.parametrize(
    ("params", "expected_mode"),
    [
        ({"mode": "code.normal", "work_mode": "code"}, "code"),
        ({"mode": "team", "work_mode": "work"}, "team"),
        ({"mode": "agent.plan", "work_mode": "work"}, "agent"),
    ],
)
@pytest.mark.asyncio
async def test_flash_affinity_does_not_override_code_team_or_plan(
    flash_config, monkeypatch, tmp_path, params, expected_mode,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    session_id = f"flash-sticky-{expected_mode}"
    session_metadata.init_session_metadata(
        session_id=session_id,
        channel_id="web",
        mode="agent",
        work_mode="work",
        adapter_mode="flash",
        sessions_root=tmp_path,
    )
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda _: tmp_path)
    agent = object()
    manager = SimpleNamespace(
        wait_for_session_prewarm=AsyncMock(),
        get_agent=AsyncMock(return_value=agent),
    )
    ctx = SimpleNamespace(services=SimpleNamespace(agent_manager=manager))
    request = make_request({**params, "query": "hello"}, request_id=session_id)
    request.session_id = session_id

    mode, _, selected = await _default._prepare_code_mode_chat_turn(
        ctx, request, "web",
    )

    assert mode == expected_mode
    assert selected is agent
    assert manager.get_agent.await_args.kwargs["mode"] == expected_mode
    persisted = session_metadata.get_session_metadata(
        session_id,
        cache_bust=True,
        enable_writeback=False,
        sessions_root=tmp_path,
    )
    assert persisted["adapter_mode"] == "flash"


@pytest.mark.asyncio
async def test_session_create_prewarm_uses_flash_factory_and_business_mode():
    pool = object.__new__(TenantAgentPool)
    agent = SimpleNamespace(prepare_session=AsyncMock())
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=agent))
    pool.get_agent_manager = AsyncMock(return_value=manager)
    request = make_request({"mode": "agent", "work_mode": "work"})

    await pool.prewarm_session_in_background(
        request=request,
        session_id="flash-prewarm-session",
        channel_id="officeclaw",
        mode="agent",
        adapter_mode="flash",
        project_dir=None,
    )
    task = pool._session_prewarm_tasks["flash-prewarm-session"]
    await task

    manager.get_agent.assert_awaited_once_with(
        channel_id="officeclaw",
        mode="flash",
        project_dir=None,
        sub_mode=None,
        request=request,
    )
    agent.prepare_session.assert_awaited_once_with(
        session_id="flash-prewarm-session",
        channel_id="officeclaw",
        mode="agent",
        adapter_mode="flash",
        project_dir=None,
    )


@pytest.mark.asyncio
async def test_concurrent_prewarm_is_deduplicated_by_session():
    session_id = "flash-prewarm-concurrent"
    pool = object.__new__(TenantAgentPool)
    first_entered = asyncio.Event()
    release = asyncio.Event()
    factory_calls = 0
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def get_agent(**_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            first_entered.set()
            await release.wait()
        return agent

    manager = SimpleNamespace(get_agent=get_agent)
    pool.get_agent_manager = AsyncMock(return_value=manager)
    request = make_request({"mode": "agent", "work_mode": "work"})

    await pool.prewarm_session_in_background(
        request=request,
        session_id=session_id,
        channel_id="web",
        mode="agent",
        adapter_mode="flash",
        project_dir=None,
    )
    first_task = pool._session_prewarm_tasks[session_id]
    await first_entered.wait()

    # A duplicate session.create joins the same in-flight prewarm instead of
    # scheduling another adapter factory creation.
    await pool.prewarm_session_in_background(
        request=request,
        session_id=session_id,
        channel_id="web",
        mode="agent",
        adapter_mode="flash",
        project_dir=None,
    )
    assert pool._session_prewarm_tasks[session_id] is first_task
    assert factory_calls == 1

    waiter = asyncio.create_task(pool.wait_for_session_prewarm(session_id))
    await asyncio.sleep(0)
    assert not waiter.done()

    release.set()
    await first_task
    await waiter
    assert factory_calls == 1
    assert agent.prepare_session.await_count == 1
    assert session_id not in pool._session_prewarm_tasks


@pytest.mark.asyncio
async def test_first_tenant_chat_waits_for_session_prewarm(
    flash_config, monkeypatch, tmp_path,
):
    session_id = "flash-prewarm-chat-race"
    pool = object.__new__(TenantAgentPool)
    first_entered = asyncio.Event()
    release = asyncio.Event()
    factory_calls = 0
    get_agent_calls = 0
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def get_agent(**_kwargs):
        nonlocal factory_calls, get_agent_calls
        get_agent_calls += 1
        if factory_calls == 0:
            first_entered.set()
            await release.wait()
            factory_calls += 1
        return agent

    manager = SimpleNamespace(
        get_agent=get_agent,
        wait_for_session_prewarm=AsyncMock(),
    )
    pool.get_agent_manager = AsyncMock(return_value=manager)
    request = make_request(
        {"mode": "agent", "work_mode": "work", "query": "hello"},
        request_id="flash-prewarm-chat-race",
    )
    request.session_id = session_id

    await pool.prewarm_session_in_background(
        request=request,
        session_id=session_id,
        channel_id="web",
        mode="agent",
        adapter_mode="flash",
        project_dir=None,
    )
    prewarm_task = pool._session_prewarm_tasks[session_id]
    await first_entered.wait()

    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(
        _default,
        "_get_tenant_agent_manager",
        AsyncMock(return_value=manager),
    )
    ctx = SimpleNamespace(
        services=SimpleNamespace(
            tenant_pool=Mock(return_value=pool),
            agent_manager=manager,
        )
    )

    chat = asyncio.create_task(
        _default._prepare_tenant_code_mode_chat_turn(ctx, request, "web")
    )
    await asyncio.sleep(0)
    assert not chat.done()
    assert factory_calls == 0

    release.set()
    mode, sub_mode, selected = await chat
    await prewarm_task

    assert (mode, sub_mode, selected) == ("flash", None, agent)
    assert factory_calls == 1
    assert get_agent_calls == 2
    assert request.params["mode"] == "agent"
    assert request.resolved_adapter_mode == "flash"
    assert agent.prepare_session.await_count == 1
