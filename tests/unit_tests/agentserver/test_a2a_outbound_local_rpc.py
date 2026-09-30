# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-less sidecar coverage for the A2A outbound local RPC fallback."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.a2a_outbound_tools import (
    A2AOutboundToolkit,
    GatewayA2AOutboundToolBackend,
)
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
    get_acp_output_manager,
)
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.server.runtime import a2a_outbound_local_rpc as local_rpc
from jiuwenswarm.server.runtime.a2a_outbound_local_rpc import (
    handle_a2a_outbound_tool_push,
    mark_gateway_manager_present,
)


@pytest.fixture(autouse=True)
def _reset_manager_state():
    manager = get_acp_output_manager()
    manager.reset_state()
    yield
    manager.reset_state()


def _chunk_for(method: str, params: dict, *, session_id: str | None):
    msg = {
        "request_id": "acp_out_test",
        "channel_id": "officeclaw",
        "response_kind": "acp.output_request",
        "body": {
            "jsonrpc": "2.0",
            "id": "acp-test-1",
            "method": method,
            "params": params,
        },
    }
    if session_id:
        msg["session_id"] = session_id
    chunk = parse_agent_server_wire_chunk(build_server_push_wire(msg))
    return chunk, msg.get("session_id")


def test_non_a2a_push_is_not_handled() -> None:
    chunk = SimpleNamespace(
        payload={"event_type": "chat.delta", "content": "hi"},
        channel_id="officeclaw",
    )
    assert asyncio.run(
        handle_a2a_outbound_tool_push(chunk=chunk, session_id="s1")
    ) is False


@pytest.mark.asyncio
async def test_gateway_manager_presence_defers_to_real_gateway() -> None:
    mark_gateway_manager_present()
    try:
        chunk, session_id = _chunk_for(
            "a2a.outbound.tool.find_agents", {"query": ""}, session_id="s1"
        )
        assert (
            await handle_a2a_outbound_tool_push(chunk=chunk, session_id=session_id)
            is False
        )
    finally:
        local_rpc._GATEWAY_MANAGER_PRESENT = False


@pytest.mark.asyncio
async def test_find_agents_without_session_is_rejected_cleanly() -> None:
    chunk, _ = _chunk_for(
        "a2a.outbound.tool.find_agents", {"query": ""}, session_id=None
    )
    handled = await handle_a2a_outbound_tool_push(chunk=chunk, session_id=None)
    # The push is consumed (handled) and resolved as a DISPATCH_REJECTED error
    # rather than left pending — either way no stall and no unhandled frame.
    assert handled is True
    assert get_acp_output_manager().pending_count == 0


@pytest.mark.asyncio
async def test_dispatch_task_is_rejected_not_stalled(monkeypatch) -> None:
    chunk, session_id = _chunk_for(
        "a2a.outbound.tool.dispatch_task",
        {"agent_id": "a1", "task": "work", "mode": "sync"},
        session_id="s1",
    )
    handled = await handle_a2a_outbound_tool_push(
        chunk=chunk, session_id=session_id
    )
    assert handled is True
    assert get_acp_output_manager().pending_count == 0


@pytest.mark.asyncio
async def test_find_agents_local_watchdog(monkeypatch) -> None:
    """A silent topology must fail fast via the local RPC timeout, not stall."""
    manager = get_acp_output_manager()

    async def delivered_but_silent(_msg):
        return 1  # push "delivered", nobody ever answers (Sept-7 incident shape)

    monkeypatch.setattr(manager, "_send_push_callback", delivered_but_silent)
    monkeypatch.setattr(
        GatewayA2AOutboundToolBackend,
        "ready",
        property(lambda _self: True),
    )
    monkeypatch.setenv("A2A_OUTBOUND_TRANSPORT_TIMEOUT_SECONDS", "0.2")

    toolkit = A2AOutboundToolkit(
        GatewayA2AOutboundToolBackend(),
        runtime_route=lambda: ("s1", "officeclaw"),
    )
    result = await toolkit.find_agents(query="")

    assert result["ok"] is False
    assert result["error_code"] == "A2A_OUTBOUND_MANAGER_UNAVAILABLE"
    assert manager.pending_count == 0
