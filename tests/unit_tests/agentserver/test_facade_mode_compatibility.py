# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise real facade entry points without model calls or persistent sessions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import _shared, chat
from jiuwenswarm.server.runtime.agent_adapter import interface
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session.session_manager import SessionManager


class RecordingAdapter:
    def __init__(self, mode):
        self.mode = mode
        self.create_instance = AsyncMock()
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
