# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``client_tool.*`` requests: one interface to declare, answer and cancel client tool calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.common.cancel_sources import CANCEL_SOURCE_NEW_MESSAGE
from jiuwenswarm.common.e2a.constants import E2A_CANCEL_SOURCE_CLIENT_DISCONNECT, E2A_INTERNAL_CANCEL_SOURCE_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.client_tool_adapter import ClientToolAdapter, ClientToolStateStore
from jiuwenswarm.server.handlers import client_tools as handlers
from jiuwenswarm.server.handlers.client_tools import (
    handle_client_tool_request,
    settle_client_tools_on_chat_cancel,
)

AGENT_TOOL = "client__read_page"

# Session metadata the handler reads, by session id.
_SESSIONS: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _sessions(monkeypatch):
    _SESSIONS.clear()
    _SESSIONS["s1"] = {"mode": "agent", "channel_id": "web"}
    monkeypatch.setattr(handlers, "_session_metadata", lambda session_id: dict(_SESSIONS.get(session_id, {})))


class _Host:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.delivered: list[str] = []

    async def emit(self, session_id, payload) -> None:
        self.events.append(payload)

    async def deliver(self, call, envelope) -> None:
        self.delivered.append(call.call_id)


def _runtime(tmp_path, host: _Host) -> SimpleNamespace:
    adapter = ClientToolAdapter(store=ClientToolStateStore(lambda: tmp_path), emit=host.emit, deliver=host.deliver)
    return SimpleNamespace(client_tool_adapter=adapter)


async def _request(runtime, method: ReqMethod, *, session_id: str = "s1", user_id: str = "", **params):
    sink = SimpleNamespace(send_response=AsyncMock())
    ctx = SimpleNamespace(
        request=AgentRequest(
            request_id="r1", channel_id="web", session_id=session_id, req_method=method, params=params,
            user_id=user_id,
        ),
        sink=sink,
        services=SimpleNamespace(execution_runtime=lambda: runtime),
    )
    await handle_client_tool_request(ctx)
    [response] = [call.args[0] for call in sink.send_response.await_args_list]
    return response


async def test_a_client_declares_tools_answers_a_call_and_sees_it_settle(tmp_path) -> None:
    host = _Host()
    runtime = _runtime(tmp_path, host)

    registered = await _request(runtime, ReqMethod.CLIENT_TOOL_REGISTER, client_id="pane-1",
                                tools=[{"name": "read_page"}])
    assert registered.ok and registered.payload["tools"][0]["agent_tool_name"] == AGENT_TOOL

    await runtime.client_tool_adapter.open_call("s1", "call-1", AGENT_TOOL, {})
    pending = await _request(runtime, ReqMethod.CLIENT_TOOL_LIST_PENDING, client_id="pane-1")
    assert [event["call_id"] for event in pending.payload["pending"]] == ["call-1"]

    answered = await _request(runtime, ReqMethod.CLIENT_TOOL_RESULT, client_id="pane-1",
                              call_id="call-1", result="page one")
    assert answered.ok and answered.payload["status"] == "completed"

    again = await _request(runtime, ReqMethod.CLIENT_TOOL_RESULT, call_id="call-1", result="page one")
    assert not again.ok and again.payload["code"] == "CLIENT_TOOL_CALL_SETTLED"


async def test_a_cancelled_call_refuses_its_result(tmp_path) -> None:
    host = _Host()
    runtime = _runtime(tmp_path, host)
    await runtime.client_tool_adapter.register("s1", "pane-1", [{"name": "read_page"}])
    await runtime.client_tool_adapter.open_call("s1", "call-1", AGENT_TOOL, {})

    cancelled = await _request(runtime, ReqMethod.CLIENT_TOOL_CANCEL, call_id="call-1", reason="closed")
    late = await _request(runtime, ReqMethod.CLIENT_TOOL_RESULT, call_id="call-1", result="page one")

    assert cancelled.ok and cancelled.payload["status"] == "cancelled"
    assert late.payload["code"] == "CLIENT_TOOL_CALL_SETTLED"
    assert host.delivered == []


async def test_a_request_without_a_result_or_an_adapter_is_refused(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Host())

    missing = await _request(runtime, ReqMethod.CLIENT_TOOL_RESULT, call_id="call-1")
    unavailable = await _request(SimpleNamespace(), ReqMethod.CLIENT_TOOL_LIST_PENDING)

    assert missing.payload["code"] == "CLIENT_TOOL_BAD_REQUEST"
    assert unavailable.payload["code"] == "CLIENT_TOOL_UNAVAILABLE"


def _cancel(intent: str, source: str = "") -> AgentRequest:
    return AgentRequest(
        request_id="interrupt-1", channel_id="web", session_id="s1", req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": intent, "session_id": "s1"},
        metadata={E2A_INTERNAL_CANCEL_SOURCE_KEY: source} if source else None,
    )


async def test_stopping_the_conversation_cancels_and_pausing_input_does_not(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Host())
    adapter = runtime.client_tool_adapter
    await adapter.register("s1", "pane-1", [{"name": "read_page"}])
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {})

    await settle_client_tools_on_chat_cancel(runtime, _cancel("supplement"))
    assert len(adapter.list_pending("s1")) == 1

    await settle_client_tools_on_chat_cancel(runtime, _cancel("cancel"))
    assert adapter.list_pending("s1") == []


async def test_a_new_message_replacing_the_turn_leaves_the_calls_running(tmp_path) -> None:
    # Regression (E2E): Gateway interrupts the session with intent=cancel before every new
    # chat.send; that cancelled the calls the user was still waiting for.
    runtime = _runtime(tmp_path, _Host())
    adapter = runtime.client_tool_adapter
    await adapter.register("s1", "pane-1", [{"name": "read_page"}])
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {})

    await settle_client_tools_on_chat_cancel(runtime, _cancel("cancel", CANCEL_SOURCE_NEW_MESSAGE))

    assert [event["call_id"] for event in adapter.list_pending("s1")] == ["call-1"]


async def test_a_dropped_client_starts_the_grace_period_instead_of_cancelling(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Host())
    adapter = runtime.client_tool_adapter
    adapter.on_client_detached = MagicMock()
    await adapter.register("s1", "pane-1", [{"name": "read_page"}])
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {})

    await settle_client_tools_on_chat_cancel(runtime, _cancel("cancel", E2A_CANCEL_SOURCE_CLIENT_DISCONNECT))

    adapter.on_client_detached.assert_called_once_with("s1")
    assert len(adapter.list_pending("s1")) == 1


@pytest.mark.parametrize(("owner", "requester"), [
    ("alice", "bob"),   # another user's Session
    ("alice", ""),      # an anonymous request never reaches an owned Session
])
async def test_a_session_of_someone_else_reads_as_not_found(tmp_path, owner: str, requester: str) -> None:
    _SESSIONS["s1"]["user_id"] = owner
    host = _Host()
    runtime = _runtime(tmp_path, host)
    await runtime.client_tool_adapter.register("s1", "pane-1", [{"name": "read_page"}])
    await runtime.client_tool_adapter.open_call("s1", "call-1", AGENT_TOOL, {})

    responses = [
        await _request(runtime, ReqMethod.CLIENT_TOOL_REGISTER, user_id=requester, client_id="x",
                       tools=[{"name": "steal"}]),
        await _request(runtime, ReqMethod.CLIENT_TOOL_RESULT, user_id=requester, call_id="call-1", result="forged"),
        await _request(runtime, ReqMethod.CLIENT_TOOL_CANCEL, user_id=requester, call_id="call-1"),
        await _request(runtime, ReqMethod.CLIENT_TOOL_LIST_PENDING, user_id=requester),
    ]

    assert [response.payload["code"] for response in responses] == ["CLIENT_TOOL_SESSION_NOT_FOUND"] * 4
    assert [spec.name for spec in runtime.client_tool_adapter.specs("s1")] == ["read_page"]
    assert [event["call_id"] for event in runtime.client_tool_adapter.list_pending("s1")] == ["call-1"]


async def test_the_owner_of_a_session_is_served(tmp_path) -> None:
    _SESSIONS["s1"]["user_id"] = "alice"
    runtime = _runtime(tmp_path, _Host())

    response = await _request(runtime, ReqMethod.CLIENT_TOOL_REGISTER, user_id="alice", client_id="pane-1",
                              tools=[{"name": "read_page"}])

    assert response.ok


async def test_a_session_that_does_not_exist_reads_as_not_found(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Host())

    response = await _request(runtime, ReqMethod.CLIENT_TOOL_REGISTER, session_id="ghost", client_id="pane-1",
                              tools=[{"name": "read_page"}])

    assert response.payload["code"] == "CLIENT_TOOL_SESSION_NOT_FOUND"
    assert not any(tmp_path.rglob("client_tools.json"))


async def test_a_session_id_that_names_a_path_is_refused(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Host())

    response = await _request(runtime, ReqMethod.CLIENT_TOOL_REGISTER, session_id="../../outside",
                              client_id="pane-1", tools=[{"name": "read_page"}])

    assert response.payload["code"] == "CLIENT_TOOL_BAD_REQUEST"
    assert not any(tmp_path.rglob("client_tools.json"))
