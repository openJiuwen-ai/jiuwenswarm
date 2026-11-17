# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

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
from copy import deepcopy
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import DeepAgentConfig


# --- from test_permission_facade_admission.py ---

@pytest.fixture
def borrowed_facade(lifecycle, monkeypatch):
    h = lifecycle
    monkeypatch.setattr(interface.JiuWenSwarm, "_prepare_skill_library", lambda _: None)
    monkeypatch.setattr(interface, "SkillManager", Mock())
    monkeypatch.setattr(interface, "append_history_record", Mock())
    monkeypatch.setattr(interface, "restore_chat_send_equipment_params", Mock())
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

# --- from test_permission_full_reload.py ---

class _ReloadRail(AgentRail):
    def __init__(self):
        super().__init__()
        self.initialized = 0

    def init(self, _agent):
        self.initialized += 1

def _isolate_optional_providers(monkeypatch, h):
    """Stub external providers, never the Host reload or SDK lifecycle."""
    adapter = h.adapter
    rail = _ReloadRail()

    async def snapshot(config, _env):
        adapter._config_base_cache = deepcopy(config)
        adapter._config_cache = deepcopy(config.get("react", {}))
        return config

    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", snapshot)
    for name in (
        "_cancel_skill_retrieval_build_after_reload", "_try_init_a2x_client",
        "_sync_skill_retrieval_prompt_rail_for_runtime", "load_user_rails",
        "_sync_mcp_servers_for_runtime", "_load_active_packages", "_handle_memory_rail_by_config",
    ):
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in (
        "_sync_a2x_runtime_state", "_sync_multimodal_tools_for_runtime",
        "_sync_paid_search_tool_for_runtime", "_sync_symphony_tools_for_runtime",
        "_sync_skill_retrieval_tools_for_runtime", "_sync_active_evolution_review_agent_after_reload",
    ):
        monkeypatch.setattr(adapter, name, Mock())
    monkeypatch.setattr(adapter, "_create_model", Mock(return_value=adapter._model))
    monkeypatch.setattr(adapter, "_get_current_agent_rails", Mock(return_value=[rail]))
    monkeypatch.setattr(adapter, "_make_deep_agent_config", lambda **kw: DeepAgentConfig(
        rails=kw["rails"], auto_create_workspace=False,
    ))
    return rail

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "configure", "ensure_initialized"])
async def test_full_reload_configure_is_admitted_only_after_real_initialization(lifecycle, monkeypatch, failure):
    h = lifecycle
    h.change()
    await h.reload()
    old_permission = h.adapter._permission_rail
    added = _isolate_optional_providers(monkeypatch, h)
    h.desired["react"] = {"agent_name": "updated-name"}
    configured = Mock(wraps=h.instance.configure)
    monkeypatch.setattr(h.instance, "configure", configured)
    reached, release = asyncio.Event(), asyncio.Event()
    if failure == "configure":
        def configure_then_fail(config):
            configured._mock_wraps(config)
            raise RuntimeError("configure failed after mutation")
        configured.side_effect = configure_then_fail
    initialize = h.instance.ensure_initialized

    async def initialize_then_pause():
        await initialize()
        reached.set()
        await release.wait()
        if failure == "ensure_initialized":
            raise RuntimeError("initialize failed after registration")

    monkeypatch.setattr(h.instance, "ensure_initialized", initialize_then_pause)
    task = asyncio.create_task(h.adapter.reload_agent_config(h.desired, reload_scopes={"models"}))
    requesting = None
    try:
        if failure != "configure":
            await asyncio.wait_for(reached.wait(), 5)
            requesting = asyncio.create_task(h.request())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert h.observed == [] and not requesting.done()
            release.set()
        if failure:
            with pytest.raises(RuntimeError, match="failed after"):
                await asyncio.wait_for(task, 5)
            if requesting:
                with pytest.raises(RuntimeError, match="permission_session_isolated"):
                    await requesting
            assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
            assert h.instance.configured_rails() == [] and h.callbacks() == []
        else:
            await asyncio.wait_for(task, 5)
            await asyncio.wait_for(requesting, 5)
            assert h.adapter._permission_rail is not old_permission
            assert h.instance.is_registered_rail(added) and added.initialized == 1
            assert h.permissions() == [h.adapter._permission_rail]
            assert len(h.observed) == 1
        configured.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(task, *([requesting] if requesting else []), return_exceptions=True)

@pytest.mark.asyncio
async def test_full_reload_busy_does_not_call_any_live_provider(lifecycle, monkeypatch):
    h = lifecycle
    h.change()
    await h.reload()
    _isolate_optional_providers(monkeypatch, h)
    h.desired["react"] = {"agent_name": "updated-name"}
    h.adapter._mark_session_active("lifecycle")
    try:
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.adapter.reload_agent_config(h.desired, reload_scopes={"models"})
        h.adapter._create_model.assert_not_called()
        h.adapter._sync_mcp_servers_for_runtime.assert_not_awaited()
        assert not h.adapter._permission_state.permission_isolated
    finally:
        h.adapter._unmark_session_active("lifecycle")

