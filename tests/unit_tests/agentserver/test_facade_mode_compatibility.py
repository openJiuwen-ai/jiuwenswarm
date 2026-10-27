# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise real facade entry points without model calls or persistent sessions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import _default, _shared, chat
from jiuwenswarm.server.runtime.agent_adapter import interface
from jiuwenswarm.server.runtime.agent_manager import AgentManager, _make_agent_cache_key
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


class RecordingAdapter:
    def __init__(self, mode):
        self.mode = mode
        self.create_instance = AsyncMock()
        self.handle_heartbeat = AsyncMock(return_value=None)
        self.interrupts = []
        self.messages = []
        self._session_adapters = {}

    async def process_interrupt(self, request):
        self.interrupts.append(request)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"event_type": "chat.interrupt_result", "success": True},
        )

    async def process_message_stream_impl(self, request, inputs):
        self.messages.append((request, dict(inputs)))
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.done"},
            is_complete=True,
        )

    async def process_message_impl(self, request, inputs):
        self.messages.append((request, dict(inputs)))
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id,
            ok=True, payload={"event_type": "chat.done"},
        )


@pytest.fixture
def facades(monkeypatch, tmp_path):
    created = []

    def create_adapter(_sdk, *, mode, **_kwargs):
        adapter = RecordingAdapter(mode)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(interface, "create_adapter", create_adapter)
    monkeypatch.setattr(interface, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface, "get_config", lambda: {})
    monkeypatch.setattr(interface, "is_auto_memory_enabled", lambda *_: False)
    monkeypatch.setattr(interface, "_schedule_symphony_session_feedback", Mock())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.workspace.policy_reload.reload_quota_policies_from_gateway_db",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers._team_session_has_runtime",
        AsyncMock(return_value=False),
    )

    def build():
        facade = object.__new__(interface.JiuWenSwarm)
        facade._adapter = None
        facade._adapter_mode = None
        facade._workspace_dir = str(tmp_path)
        facade._agent_id = facade._service_id = None
        facade._skill_manager = Mock()
        facade._personal_context_runtime_enabled = False
        facade._session_manager = SessionManager(telemetry=Mock())
        facade._permission_response_ledger = interface.PermissionResponseLedger()
        facade._append_history_record = Mock()
        facade._materialize_enterprise_attachments = AsyncMock()
        facade._bind_tenant_request_context = Mock(return_value=(None, None))
        facade._reset_tenant_request_context = Mock()
        facade._cancel_team_work_for_session = AsyncMock()
        facade._build_inputs = lambda req: (
            {"query": req.params.get("query", "")}, "disabled", req.params.get("query", ""),
        )
        return facade

    return build, created


def make_request(mode, *, method=ReqMethod.CHAT_SEND, session_id="compat-session", **params):
    if mode is not None:
        params["mode"] = mode
    return AgentRequest(
        request_id=f"compat-{session_id}",
        channel_id="officeclaw",
        session_id=session_id,
        req_method=method,
        params=params,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,plan_entry_source", [
    ("team", None), ("team.plan", "e2a"), ("code.team", None),
    ("agent.plan", "e2a"), ("agent.plan", None), ("agent", None),
    ("flash", None), (None, None), ("", None),
])
async def test_cold_facade_stream_preserves_routed_mode(facades, tmp_path, mode, plan_entry_source):
    build, created = facades
    params = {"query": "hello", "project_dir": str(tmp_path)}
    if plan_entry_source is not None:
        params["plan_entry_source"] = plan_entry_source
    incoming = make_request(
        mode, **params,
    )
    manager = object.__new__(AgentManager)
    manager_mode, sub_mode, project_dir = manager._agent_lookup_from_request(incoming)
    assert project_dir == str(tmp_path)
    facade = build()
    await facade.create_instance(mode=manager_mode, sub_mode=sub_mode)

    chunks = await asyncio.wait_for(
        _collect(facade.process_message_stream(incoming)), timeout=5,
    )

    assert chunks[-1].is_complete
    assert not any((chunk.payload or {}).get("event_type") == "chat.error" for chunk in chunks)
    assert created[0].messages[0][0] is incoming
    assert created[0].messages[0][1]["query"] == "hello"
    assert created[0].create_instance.await_args.kwargs["mode"] == manager_mode
    assert incoming.params.get("mode") == mode


async def _collect(stream):
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("original_mode", ["team", "agent", "code", "flash"])
async def test_parameterless_rebuild_keeps_business_mode(facades, original_mode):
    build, created = facades
    facade = build()
    await facade.create_instance(mode=original_mode)
    if original_mode == "team":
        assert facade._ensure_adapter(mode="agent") is created[0]
    await facade.create_instance()
    assert len(created) == 1
    assert [call.kwargs["mode"] for call in created[0].create_instance.await_args_list] == [
        original_mode, original_mode,
    ]


@pytest.mark.parametrize("original_mode,new_mode", [
    ("agent", "flash"), ("flash", "agent"), ("team", "flash"),
    ("code", "agent"), ("team", "code"), ("flash", "code"),
])
def test_genuine_cross_implementation_reuse_remains_rejected(facades, original_mode, new_mode):
    build, created = facades
    facade = build()
    first = facade._ensure_adapter(mode=original_mode)
    with pytest.raises(RuntimeError, match="adapter mode mismatch"):
        facade._ensure_adapter(mode=new_mode)
    assert facade._adapter is first
    assert len(created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_mode", ["flash", "code", "agent"])
@pytest.mark.parametrize("incoming_mode", ["agent.plan", None, ""])
@pytest.mark.parametrize("intent", ["pause", "resume", "supplement", "cancel"])
async def test_legacy_interrupt_reaches_bound_adapter(facades, bound_mode, incoming_mode, intent):
    build, created = facades
    facade = build()
    await facade.create_instance(mode=bound_mode)
    incoming = make_request(incoming_mode, method=ReqMethod.CHAT_CANCEL, intent=intent)

    response = await facade.process_message(incoming)

    assert response.payload["success"] is True
    assert created[0].interrupts == [incoming]
    assert len(created) == 1
    assert facade._adapter_mode == bound_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("flash", "flash"), ("agent.plan", "agent"), (None, "agent")])
async def test_interrupt_without_bound_adapter_keeps_request_fallback(facades, mode, expected):
    build, created = facades
    incoming = make_request(mode, method=ReqMethod.CHAT_CANCEL, intent="pause")
    response = await build().process_message(incoming)
    assert response.payload["success"] is True
    assert len(created) == 1
    assert created[0].mode == expected
    assert created[0].interrupts == [incoming]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.plan", None])
@pytest.mark.parametrize("intent", ["pause", "resume", "supplement", "cancel"])
async def test_tenant_interrupt_uses_actual_session_owner(facades, monkeypatch, mode, intent):
    build, created = facades
    owner, unrelated = build(), build()
    await owner.create_instance(mode="flash")
    await unrelated.create_instance(mode="agent")
    owner._adapter._session_adapters["owned-session"] = object()
    unrelated._adapter._session_adapters["other-session"] = object()
    manager = SimpleNamespace(agents={"officeclaw": {"agent::other": unrelated, "flash::owned": owner}})
    pool = SimpleNamespace(
        resolve_control_rpc_tenant=lambda req, agent_id, service_id: (agent_id, service_id),
        get_agent_manager=AsyncMock(return_value=manager),
    )
    monkeypatch.setattr(_shared, "get_config", lambda: {"flash": {"enabled": True}})
    incoming = make_request(mode, method=ReqMethod.CHAT_CANCEL, session_id="owned-session", intent=intent)
    ctx = SimpleNamespace(
        request=incoming,
        services=SimpleNamespace(tenant_pool=lambda: pool, session_stream_tasks={}),
        sink=SimpleNamespace(send_wire=AsyncMock()),
    )

    await chat.handle_chat_cancel_dispatch(ctx)

    assert created[0].interrupts == [incoming]
    assert created[1].interrupts == []
    assert len(created) == 2
    ctx.sink.send_wire.assert_awaited_once()


@pytest.fixture
def resume_routes(facades, monkeypatch, tmp_path):
    """Keep real owner scans, cache routing, metadata and facade entry points."""
    build, created = facades
    config = {"flash": {"enabled": False}}
    monkeypatch.setattr(_shared, "get_config", lambda: config)
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda _: tmp_path)
    monkeypatch.setattr("jiuwenswarm.common.utils.resolve_tenant_sessions_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.ensure_interface_deep_and_checkpointer", AsyncMock(),
    )
    monkeypatch.setattr(_default, "_ensure_code_mode_state", AsyncMock(return_value=False))
    monkeypatch.setattr(_default, "_check_post_process_plan_exit", AsyncMock())
    manager = object.__new__(AgentManager)
    manager.agents = {"officeclaw": {}}
    manager._workspace_key = "default"
    manager._env_service_id = manager._env_agent_id = None
    manager._agent_borrowers = {}
    manager._pending_tui_retirements = {}
    manager.wait_for_session_prewarm = AsyncMock()
    pool = object.__new__(TenantAgentPool)
    pool.require_officeclaw_agent = Mock(return_value=None)
    pool.iter_agent_managers_nowait = lambda: [manager]
    pool.resolve_control_rpc_tenant = lambda req, aid, sid: (aid, sid)
    pool.get_agent_manager = AsyncMock(return_value=manager)
    pool._ensure_agent_manager = AsyncMock(return_value=manager)
    pool._refresh_agent_manager_cache = AsyncMock()
    ctx = SimpleNamespace(
        services=SimpleNamespace(
            agent_manager=manager, tenant_pool=lambda: pool, session_stream_tasks={},
        ),
        sink=SimpleNamespace(send_chunk=AsyncMock(return_value=True), send_unary=AsyncMock()),
    )

    async def add(mode, sub_mode=None):
        facade = build()
        await facade.create_instance(mode=mode, sub_mode=sub_mode)
        key = _make_agent_cache_key(mode, sub_mode or "", str(tmp_path))
        manager.agents["officeclaw"][key] = facade
        return facade

    def initialize(sid, adapter_mode="agent"):
        session_metadata.init_session_metadata(
            session_id=sid, channel_id="officeclaw", mode="agent", work_mode="work",
            adapter_mode=adapter_mode, project_dir=str(tmp_path), sessions_root=tmp_path,
        )

    return SimpleNamespace(
        add=add, initialize=initialize, config=config, manager=manager,
        pool=pool, ctx=ctx, created=created,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["handler", "manager", "pool", "handler_pool"])
@pytest.mark.parametrize("source", [
    "permission_interrupt", "confirm_interrupt", "ask_user_interrupt",
    "evolution_interrupt", "skill_evolution_approval",
])
@pytest.mark.parametrize("bound_mode", ["agent", "flash"])
@pytest.mark.parametrize("incoming_mode", ["agent.plan", None])
async def test_resume_reaches_paused_owner_stream(
    resume_routes, tmp_path, path, source, bound_mode, incoming_mode,
):
    routes = resume_routes
    routes.initialize("paused-session")
    owner = await routes.add(bound_mode)
    other_mode = "flash" if bound_mode == "agent" else "agent"
    unrelated = await routes.add(other_mode)
    # A real paused interaction session, discoverable by the production scans.
    owner._adapter._instance = SimpleNamespace(
        _interaction_session=SimpleNamespace(session_id=lambda: "paused-session"),
    )
    owner._adapter._read_session_interrupt_state = lambda: {"tool_call_id": "tool-1"}
    routes.config["flash"]["enabled"] = bound_mode == "agent"
    request = make_request(
        incoming_mode, session_id="paused-session", source=source,
        answers=[{"option_id": "allow"}], request_id="tool-1", query="continue",
        project_dir=str(tmp_path), work_mode="work",
    )
    if source == "skill_evolution_approval":
        request.params["evolution_meta"] = {"approval_transport": "interrupt"}
    # The pool shortcut can receive a decision computed by an upstream handler.
    request.resolved_adapter_mode = other_mode
    request.resolved_adapter_source = "flash_enabled_default" if other_mode == "flash" else "default"
    if path == "handler":
        _, _, selected = await _default._prepare_code_mode_chat_turn(
            routes.ctx, request, "officeclaw",
        )
        assert selected is owner
        chunks = await _collect(selected.process_message_stream(request))
    elif path == "handler_pool":
        await _default._handle_stream_impl(routes.ctx, request)
        chunks = [call.args[0] for call in routes.ctx.sink.send_chunk.await_args_list]
    else:
        target = routes.manager if path == "manager" else routes.pool
        chunks = await _collect(target.process_message_stream(request))
    assert chunks[-1].is_complete
    assert not any((chunk.payload or {}).get("event_type") == "chat.error" for chunk in chunks)
    assert owner._adapter.messages[0][0] is request
    assert unrelated._adapter.messages == []
    assert request.resolved_adapter_mode == bound_mode
    assert request.resolved_adapter_source == "session_owner"
    assert len(routes.created) == 2
    if path in {"handler", "handler_pool"}:
        metadata = session_metadata.get_session_metadata(
            "paused-session", cache_bust=True, enable_writeback=False, sessions_root=tmp_path,
        )
        assert metadata["adapter_mode"] == bound_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["manager", "pool"])
@pytest.mark.parametrize("bound_mode", ["agent", "flash"])
@pytest.mark.parametrize("source", [
    "permission_interrupt", "confirm_interrupt", "ask_user_interrupt",
    "evolution_interrupt", "skill_evolution_approval",
])
async def test_resume_reaches_paused_owner_unary(resume_routes, tmp_path, path, bound_mode, source):
    routes = resume_routes
    owner = await routes.add(bound_mode)
    owner._adapter._sessions_awaiting_tool_permission = {"paused-session"}
    request = make_request(
        "agent.plan", session_id="paused-session", source=source,
        answers=[{"option_id": "allow"}], request_id="tool-1", query="continue",
        project_dir=str(tmp_path), work_mode="work",
    )
    if source == "skill_evolution_approval":
        request.params["evolution_meta"] = {"approval_transport": "interrupt"}
    request.resolved_adapter_mode = "flash" if bound_mode == "agent" else "agent"
    routes.config["flash"]["enabled"] = bound_mode == "agent"
    target = routes.manager if path == "manager" else routes.pool
    response = await target.process_message(request)
    assert response.ok
    assert owner._adapter.messages[0][0] is request
    assert request.resolved_adapter_mode == bound_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["manager", "handler_pool"])
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("stored_adapter,incoming_mode", [
    ("agent", "flash"), ("flash", "agent.plan"), ("agent", None), ("flash", None),
])
@pytest.mark.parametrize("source", [None, "", "explicit"])
@pytest.mark.parametrize("kind", ["skill_load", "tool_permission"])
async def test_subagent_answer_keeps_stored_binding_without_outer_pause(
    resume_routes, tmp_path, path, stream, stored_adapter, incoming_mode, source, kind,
):
    routes = resume_routes
    routes.initialize("hosted-session", stored_adapter)
    owner = await routes.add(stored_adapter)
    other = await routes.add("flash" if stored_adapter == "agent" else "agent")
    routes.config["flash"]["enabled"] = stored_adapter == "agent"
    # Hosted approvals wait in their own registry, without pausing the outer loop.
    assert routes.manager.find_agent_for_interrupt_resume_nowait("hosted-session") is None
    request = make_request(
        incoming_mode, session_id="hosted-session", request_id=f"subagent_{kind}_legacy",
        answers=["本次允许"], query="", work_mode="work",
    )
    if source is not None:
        request.params["source"] = f"subagent_{kind}" if source == "explicit" else source
    if path == "handler_pool":
        handler = _default._handle_stream_impl if stream else _default._handle_unary_impl
        await handler(routes.ctx, request)
    elif stream:
        await _collect(routes.manager.process_message_stream(request))
    else:
        response = await routes.manager.process_message(request)
        assert response.ok
    assert owner._adapter.messages[0][0] is request
    assert other._adapter.messages == []
    assert request.resolved_adapter_mode == stored_adapter
    assert len(routes.created) == 2
    await asyncio.wait_for(asyncio.to_thread(session_metadata._METADATA_QUEUE.join), timeout=5)
    metadata = session_metadata.get_session_metadata(
        "hosted-session", cache_bust=True, enable_writeback=False, sessions_root=tmp_path,
    )
    assert metadata["adapter_mode"] == stored_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_adapter", ["agent", "flash"])
@pytest.mark.parametrize("incoming_mode", ["agent.plan", None])
async def test_handler_pool_manager_preserves_session_flash_binding(
    resume_routes, tmp_path, stored_adapter, incoming_mode,
):
    routes = resume_routes
    routes.initialize("sticky-session", stored_adapter)
    deep = await routes.add("agent")
    flash = await routes.add("flash")
    routes.config["flash"]["enabled"] = False
    request = make_request(
        incoming_mode, session_id="sticky-session", query="hello",
        project_dir=str(tmp_path), work_mode="work",
    )
    await _default._handle_stream_impl(routes.ctx, request)
    expected, other = (flash, deep) if stored_adapter == "flash" else (deep, flash)
    assert expected._adapter.messages[0][0] is request
    assert other._adapter.messages == []
    assert request.resolved_adapter_mode == stored_adapter
    assert request.params["mode"] == stored_adapter
    if stored_adapter == "flash":
        assert request.resolved_adapter_source == "session_sticky"
    # Wait for the real metadata writer before asserting durable mode.
    await asyncio.wait_for(asyncio.to_thread(session_metadata._METADATA_QUEUE.join), timeout=5)
    metadata = session_metadata.get_session_metadata(
        "sticky-session", cache_bust=True, enable_writeback=False, sessions_root=tmp_path,
    )
    assert metadata["adapter_mode"] == stored_adapter
    assert metadata["mode"] == (stored_adapter if incoming_mode else "agent")


@pytest.mark.parametrize("mode,expected,sub_mode", [
    ("agent.code.normal", "code", "normal"), ("team", "team", None),
    ("agent.plan", "agent", "plan"), ("team.work.plan", "team", "plan"),
])
def test_prepared_flash_binding_cannot_override_code_team_or_plan(mode, expected, sub_mode, monkeypatch):
    monkeypatch.setattr(_shared, "get_config", lambda: {"flash": {"enabled": True}})
    request = make_request(mode, plan_entry_source="e2a", work_mode="work")
    request.resolved_adapter_mode = "flash"
    request.resolved_adapter_source = "session_sticky"
    manager = object.__new__(AgentManager)
    actual_mode, actual_sub, _ = manager._agent_lookup_from_request(request)
    assert (actual_mode, actual_sub) == (expected, sub_mode)
    assert request.resolved_adapter_mode == ("code" if expected == "code" else "agent")


@pytest.fixture
def standalone_yaml_configuration(monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    config_module.clear_config_cache()
    yield config_module, path
    config_module.clear_config_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value,config_enabled,expected", [
    ("true", False, "flash"), ("false", True, "agent"),
])
@pytest.mark.parametrize("channel,mode", [
    ("web", "agent.work.normal"), ("officeclaw", None),
    ("officeclaw", "agent.plan"), ("officeclaw", "agent.work.normal"),
])
@pytest.mark.parametrize("stream", [True, False])
async def test_flash_environment_reaches_chat_and_keeps_session_binding(
    resume_routes, standalone_yaml_configuration, monkeypatch, tmp_path, env_value, config_enabled, expected,
    channel, mode, stream,
):
    routes = resume_routes
    session_metadata.init_session_metadata(
        session_id="standalone-env", channel_id=channel, mode="agent", work_mode="work",
        adapter_mode="agent", project_dir=str(tmp_path), sessions_root=tmp_path,
    )
    deep = await routes.add("agent")
    flash = await routes.add("flash")
    if channel == "web":
        routes.manager.agents["web"] = routes.manager.agents.pop("officeclaw")
    config_module, config_file = standalone_yaml_configuration
    config_file.write_text(f"flash:\n  enabled: {str(config_enabled).lower()}\n", encoding="utf-8")
    monkeypatch.setattr(_shared, "get_config", config_module.get_config)
    monkeypatch.setenv("JIUWENSWARM_FLASH_ENABLED", env_value)

    async def send(request_id):
        config_module.clear_config_cache()
        incoming = make_request(
            mode, session_id="standalone-env", query="hello",
            work_mode="work", project_dir=str(tmp_path),
        )
        incoming.request_id = request_id
        incoming.channel_id = channel
        _, _, prepared = await _default._prepare_code_mode_chat_turn(routes.ctx, incoming, channel)
        target = routes.pool if channel == "officeclaw" else routes.manager
        if stream:
            chunks = await _collect(target.process_message_stream(incoming))
            assert chunks[-1].is_complete
            assert not any((c.payload or {}).get("event_type") == "chat.error" for c in chunks)
        else:
            assert (await target.process_message(incoming)).ok
        assert prepared is (flash if expected == "flash" else deep)
        assert incoming.resolved_adapter_mode == expected
        metadata = session_metadata.get_session_metadata(
            "standalone-env", cache_bust=True, enable_writeback=False, sessions_root=tmp_path,
        )
        assert metadata["adapter_mode"] == expected
        return incoming

    first = await send("env-first")
    selected, other = (flash, deep) if expected == "flash" else (deep, flash)
    assert selected._adapter.messages[0][0] is first
    assert other._adapter.messages == []
    if expected == "flash":
        monkeypatch.setenv("JIUWENSWARM_FLASH_ENABLED", "false")
        second = await send("env-continue")
        assert second.resolved_adapter_source == "session_sticky"
        assert selected._adapter.messages[1][0] is second
    assert len(routes.created) == 2
