"""Borrowed facades still enter the real parent/child admission boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface, interface_deep
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from tests.unit_tests.agentserver.permissions.test_permission_lifecycle_integration import (
    lifecycle as lifecycle,
)


@pytest.fixture
def borrowed_facade(lifecycle, monkeypatch):
    h = lifecycle
    monkeypatch.setattr(interface.JiuWenSwarm, "_prepare_skill_library", lambda _: None)
    monkeypatch.setattr(interface, "SkillManager", Mock())
    monkeypatch.setattr(interface, "append_history_record", Mock())
    monkeypatch.setattr(interface, "restore_chat_send_equipment_params", Mock())
    monkeypatch.setattr(interface, "_schedule_symphony_session_feedback", Mock())
    facade = interface.JiuWenSwarm()
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    parent._session_instance_mode = "agent"
    parent._session_adapters["lifecycle"] = h.adapter
    facade._adapter = parent
    facade._sdk_name = "deep"
    manager = AgentManager()
    facade.set_permissions_external_input_context_builder(
        manager.build_permissions_external_input_context,
    )
    h.adapter.set_permissions_external_input_context_builder(
        manager.build_permissions_external_input_context,
    )
    for handler in (
        "_handle_skilldev_request", "_handle_skills_request",
        "_handle_plugins_request", "_handle_package_catalog_request",
    ):
        monkeypatch.setattr(facade, handler, AsyncMock(return_value=None))
    monkeypatch.setattr(parent, "handle_heartbeat", AsyncMock(return_value=None))
    monkeypatch.setattr(facade, "reconcile_session_mcp", AsyncMock())
    monkeypatch.setattr(facade, "_build_inputs", lambda request: (
        {"query": request.params["query"], "conversation_id": request.session_id},
        None, SimpleNamespace(text=request.params["query"]),
    ))

    async def terminal(request, _inputs):
        # Only model execution is replaced: facade queue, parent selection,
        # child admission and the SDK registration graph remain real.
        h.observed.append((h.adapter._permission_state.permission_epoch, h.permissions(), h.callbacks()))
        return AgentResponse(request_id=request.request_id, channel_id="web", ok=True, payload={})

    async def stream_terminal(request, inputs):
        await terminal(request, inputs)
        yield AgentResponseChunk(
            request_id=request.request_id, channel_id="web", payload={}, is_complete=True,
        )

    monkeypatch.setattr(h.adapter, "_process_message_impl", terminal)
    monkeypatch.setattr(h.adapter, "_process_message_stream_impl", stream_terminal)
    return facade, parent


async def _send(facade, stream, *, source=None):
    params = {"mode": "agent", "query": "inspect"}
    metadata = {}
    marker = {"automation": {"kind": "heartbeat"}}
    if source == "heartbeat_params":
        params.update(marker)
    elif source == "heartbeat_metadata":
        metadata.update(marker)
    elif source == "heartbeat_nested":
        params["metadata"] = marker
    elif source is not None:
        params["source"] = source
    request = AgentRequest(
        request_id="borrowed", session_id="lifecycle", channel_id="web",
        req_method=ReqMethod.CHAT_SEND, params=params, metadata=metadata,
    )
    if stream:
        return [chunk async for chunk in facade.process_message_stream(request)]
    return await facade.process_message(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_borrowed_facade_waits_for_real_registration_cutover(
    lifecycle, borrowed_facade, monkeypatch, stream,
):
    h, (facade, _parent) = lifecycle, borrowed_facade
    h.change()
    await h.reload()
    entered, release = asyncio.Event(), asyncio.Event()
    register = h.instance.register_rail

    async def paused_register(rail):
        entered.set()
        await release.wait()
        return await register(rail)

    monkeypatch.setattr(h.instance, "register_rail", paused_register)
    h.change()
    reload_task = asyncio.create_task(h.reload())
    await asyncio.wait_for(entered.wait(), 2)
    request_task = asyncio.create_task(_send(facade, stream))
    try:
        await asyncio.sleep(0.02)
        assert not request_task.done()
        assert h.observed == []
    finally:
        release.set()
    await asyncio.wait_for(reload_task, 2)
    await asyncio.wait_for(request_task, 2)
    assert len(h.observed) == 1
    assert h.observed[0][0] == h.capture()[0]
    assert h.observed[0][1] == [h.adapter._permission_rail]
    assert h.observed[0][2]
    assert not h.adapter._permission_work_pending("lifecycle")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("source", [
    "proactive_recommendation", "heartbeat_params", "heartbeat_metadata", "heartbeat_nested",
])
async def test_internal_facade_input_cannot_install_new_permission_epoch(
    lifecycle, borrowed_facade, stream, source,
):
    h, (facade, _parent) = lifecycle, borrowed_facade
    h.change()
    await h.reload()
    old_epoch, old_rail = h.adapter._permission_state.permission_epoch, h.adapter._permission_rail
    h.change()
    if stream:
        chunks = await _send(facade, stream, source=source)
        assert any("permission_update_requires_external_input" in str(chunk.payload) for chunk in chunks)
    else:
        with pytest.raises(RuntimeError, match="permission_update_requires_external_input"):
            await _send(facade, stream, source=source)
    assert h.observed == []
    assert h.adapter._permission_state.permission_epoch == old_epoch
    assert h.permissions() == [old_rail]
    assert not h.adapter._permission_work_pending("lifecycle")
