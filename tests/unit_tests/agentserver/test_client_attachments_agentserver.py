# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer's side of client attachments: the methods it answers, the pushes, the gone calls, the refusals."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.client_attachments import directory, get_attachment_directory
from jiuwenswarm.server.client_attachments.watch import CLIENT_GONE_GRACE_SECONDS
from jiuwenswarm.server.runtime.session.session_metadata import flush_pending_writes
from tests.unit_tests.runtime.test_client_tool_control_resume import (
    _ANSWERS,
    _DocumentAgent,
    _client_request,
    _paused,
)
from tests.unit_tests.agentserver.client_attachments_support import (  # noqa: F401 - the fixture
    Socket,
    answer_async,
    answered,
    give_access,
    isolated,
    pending_call,
    renew,
    request,
    stored_metadata,
    sweep,
)

pytestmark = pytest.mark.usefixtures("isolated")


def _server() -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._current_ws = server._current_send_lock = None
    server._session_message_service = type("Mailbox", (), {"set_available": AsyncMock()})()
    server._clear_ws_acp_client_capabilities = lambda ws: None
    runtime = type("Runtime", (), {"cancel_all_inflight_work": AsyncMock()})()
    server._execution_runtime = lambda: runtime
    server._heartbeat_runtime = type("Heartbeat", (), {"execution": type(
        "Execution", (), {"active_session_ids": lambda self: set()})()})()
    server._stop_scheduler = AsyncMock()
    server._session_stream_tasks = {}
    server.send_push = AsyncMock()
    return server


def test_agent_server_answers_the_attach_methods_and_a_new_gateway_link_leaves_the_attachments_alone() -> None:
    async def scenario() -> None:
        server = _server()
        old_link, new_link = Socket(), Socket()
        await server.attach_gateway_connection(old_link, asyncio.Lock())
        await server.dispatch_parsed_request(old_link, request(), asyncio.Lock())
        attachment_id = old_link.sent[-1]["payload"]["attachment_id"]

        # A Gateway that reconnects (or a restarted Gateway) needs no cleanup:
        # the attachment lasts while its client renews it.
        await server.attach_gateway_connection(new_link, asyncio.Lock())
        await server.on_gateway_disconnect(old_link, "old")
        await server.dispatch_parsed_request(new_link, renew(attachment_id), asyncio.Lock())
        assert new_link.sent[-1]["ok"] is True
        assert [c.attachment_id for c in get_attachment_directory().connections()] == [attachment_id]

    asyncio.run(scenario())


def test_agent_server_answers_each_pending_call_of_a_gone_client_by_its_interrupt(monkeypatch) -> None:
    server = _server()
    server._session_message_service = None
    seen: list[AgentRequest] = []

    class _Runtime:
        def stream(self, agent_request, **kwargs):
            seen.append(agent_request)

            async def events():
                yield RuntimeEvent(request_id=agent_request.request_id, channel_id=agent_request.channel_id,
                                   session_id=agent_request.session_id, payload={"event_type": "chat.final"},
                                   is_complete=True)

            return events()

    server._execution_runtime = lambda: _Runtime()
    monkeypatch.setattr(agent_ws_server, "build_server_push_message", lambda **kw: dict(kw))
    pending = [pending_call("session-1", "call-1"), pending_call("session-1", "call-2")]

    asyncio.run(server._answer_calls_no_client_runs("session-1", "web", "user-1", pending))

    # The answers name the calls; the turn goes on in its own mode (the Runtime reads it from the Session).
    resumed, = seen
    assert (resumed.session_id, resumed.user_id) == ("session-1", "user-1")
    assert resumed.params == {"source": "client_tool_interrupt", "answers": [
        {"interrupt_id": "interrupt-call-1", "request_id": "call-1", "result": {"status": "client_gone"}},
        {"interrupt_id": "interrupt-call-2", "request_id": "call-2", "result": {"status": "client_gone"}},
    ]}
    server.send_push.assert_awaited_once()
    assert server.send_push.await_args.args[0]["payload"] == {"event_type": "chat.final"}


class _AsksTheClient:
    """A Runtime whose turn asks its client to run ``call_id``."""

    def __init__(self, call_id: str = "call-1") -> None:
        self.call_id = call_id

    def stream(self, agent_request, **kwargs):
        async def events():
            yield RuntimeEvent(request_id=agent_request.request_id, channel_id=agent_request.channel_id,
                               session_id=agent_request.session_id,
                               payload={"event_type": "chat.client_tool_request", "request_id": self.call_id,
                                        "source": "client_tool_interrupt"})

        return events()


def _pushing_server(monkeypatch) -> tuple[AgentWebSocketServer, list[dict]]:
    server = _server()
    pushed: list[dict] = []

    async def push(message) -> bool:
        pushed.append(message)
        return True

    server.send_push = push
    server._heartbeat_runtime.retain_agent = lambda *args: None
    server._execution_runtime = lambda: _AsksTheClient()
    monkeypatch.setattr(agent_ws_server, "enqueue_history_request_completion", lambda *a, **k: None)
    return server, pushed


async def _cross_session_message(server: AgentWebSocketServer, store_path, monkeypatch) -> None:
    """A conversation sends session-1 a task (``session_send_message``), and session-1's turn runs it."""
    from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
    from jiuwenswarm.server.runtime.session.session_message_service import SessionMessageService
    from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore

    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda *a, **k: {
        "user_id": "user-1", "channel_id": "web", "mode": "agent.code.normal"})
    store = SessionMessageStore(store_path)
    service = SessionMessageService(store=store, admission=SessionRunAdmission(),
                                    execute=lambda record: asyncio.sleep(0), available=False)
    await service.start()
    server._session_message_service = service
    queued, _ = store.enqueue(
        owner_scope_id="user-1", source_session_id="session-2", source_title_snapshot="Source",
        source_request_id="request-1", source_tool_call_id="tool-1", idempotency_key=f"message-{uuid.uuid4().hex}",
        target_session_id="session-1", content="update the deck", hop_count=1, input_mode="")
    result = await server.execute_internal_session_message(
        store.claim(queued.message_id, f"execution-{uuid.uuid4().hex}", "run-1"))
    assert result.status == "waiting_user"
    await service.stop()


async def _heartbeat(server: AgentWebSocketServer) -> None:
    """session-1's Heartbeat runs, its request built from the conversation as the scheduler builds it."""
    from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HeartbeatJob, HeartbeatSchedule
    from jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler import HeartbeatSchedulerService
    from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import HeartbeatSessionResolver

    every_minute = HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 60})
    job = HeartbeatJob(id="job-1", name="check", enabled=True, channel_id="web", session_id="session-1",
                       prompt="check", schedule=every_minute, metadata={"source": "agent_tool", "user_id": "user-1"})
    scheduler = HeartbeatSchedulerService(store=None, execution_service=None,
                                          session_resolver=HeartbeatSessionResolver())
    message = scheduler._build_message(job, "heartbeat-1", 1000.0)
    await server.execute_internal_heartbeat(AgentRequest(
        request_id="heartbeat-1", channel_id=message.channel_id, session_id="session-1", user_id=message.user_id,
        req_method=message.req_method, params=dict(message.params), is_stream=True,
        metadata=dict(message.metadata)))


def _client_tool_requests(pushed: list[dict]) -> list[tuple[str, str, str]]:
    return [(m["channel_id"], (m.get("metadata") or {}).get("ws_id", ""), m["payload"]["request_id"])
            for m in pushed if m["payload"].get("event_type") == "chat.client_tool_request"]


@pytest.mark.parametrize("turn", ["session_message", "heartbeat"])
def test_a_background_turn_of_a_conversation_nobody_wrote_in_asks_its_attached_client(
    turn, monkeypatch, tmp_path,
) -> None:
    server, pushed = _pushing_server(monkeypatch)
    assert "delivery_context" not in stored_metadata("session-1")

    async def scenario() -> None:
        assert (await answer_async(request()))["ok"] is True  # the pane on ws-1 is attached to session-1
        if turn == "heartbeat":
            flush_pending_writes()  # the scheduler reads metadata.json from disk
            await _heartbeat(server)
        else:
            await _cross_session_message(server, tmp_path / "messages.sqlite3", monkeypatch)

    asyncio.run(scenario())

    assert _client_tool_requests(pushed) == [("web", "ws-1", "call-1")]


def test_a_reloaded_task_pane_gets_the_next_background_turn_on_its_new_socket(monkeypatch, tmp_path) -> None:
    server, pushed = _pushing_server(monkeypatch)

    async def scenario() -> None:
        assert (await answer_async(request(socket="ws-1")))["ok"] is True
        await _cross_session_message(server, tmp_path / "messages.sqlite3", monkeypatch)
        assert (await answer_async(request(socket="ws-2")))["ok"] is True
        await _cross_session_message(server, tmp_path / "messages.sqlite3", monkeypatch)

    asyncio.run(scenario())

    assert _client_tool_requests(pushed) == [("web", "ws-1", "call-1"), ("web", "ws-2", "call-1")]


def test_attaching_a_conversation_does_not_make_it_look_used() -> None:
    before = stored_metadata("session-1")

    async def scenario() -> None:
        assert (await answer_async(request()))["ok"] is True

    asyncio.run(scenario())

    after = stored_metadata("session-1")
    used = ("title", "message_count", "channel_id", "last_message_at", "last_user_message_at")
    assert {k: after.get(k) for k in used} == {k: before.get(k) for k in used}
    assert after["delivery_context"]["route_metadata"] == {"ws_id": "ws-1"}


def test_a_refused_attach_leaves_the_conversation_s_pushes_where_they_went() -> None:
    async def unreadable(session_id):
        raise RuntimeError("the paused agent is gone")

    give_access(pending=unreadable, client_gone=AsyncMock(), push=AsyncMock())

    async def scenario() -> None:
        assert (await answer_async(request()))["ok"] is False

    asyncio.run(scenario())

    assert "delivery_context" not in stored_metadata("session-1")


@pytest.mark.parametrize("reattached", [True, False])
def test_a_call_the_client_gone_turn_asks_again_reaches_a_client_attached_meanwhile_else_waits_for_one(
    reattached, monkeypatch,
) -> None:
    server, pushed = _pushing_server(monkeypatch)
    server._session_message_service = None
    server._execution_runtime = lambda: _AsksTheClient("call-2")
    gone: list[tuple[str, list[str]]] = []
    clock = directory.clock  # the ``isolated`` fixture's

    async def answer_as_gone(session_id, channel_id, owner_id, pending) -> None:
        gone.append((session_id, [p["request_id"] for p in pending]))

    give_access(client_gone=answer_as_gone, push=server.send_push)

    async def scenario() -> None:
        if reattached:
            assert (await answer_async(request(socket="ws-2")))["ok"] is True
        await server._answer_calls_no_client_runs(
            "session-1", "web", "user-1", [pending_call("session-1", "call-1")])
        answered("session-1", "call-1")
        pending_call("session-1", "call-2")
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)

    asyncio.run(scenario())

    if reattached:
        assert _client_tool_requests(pushed) == [("web", "ws-2", "call-2")]
        assert gone == []
    else:
        assert gone == [("session-1", ["call-2"])]


class _AsksForAnotherCall(_DocumentAgent):
    async def deliver_control_input(self, request):
        self.delivered.append(dict(request.params))
        for answer in request.params["answers"]:
            self.paused.pop(answer["request_id"], None)
        self.paused["call-c"] = _client_request("call-c")
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload=self.paused["call-c"])


@pytest.mark.parametrize("is_stream", [False, True])
def test_a_resume_whose_turn_runs_another_client_tool_sends_its_request(is_stream, monkeypatch) -> None:
    monkeypatch.setattr(agent_ws_server, "enqueue_history_request_completion", lambda *a, **k: None)
    session_id = "session-1"

    async def scenario():
        agent = _AsksForAnotherCall()
        runtime, _admission = await _paused(session_id, agent)
        server = _server()
        server._session_message_service = type(
            "Mailbox", (), {"find_waiting_for_pause": AsyncMock(return_value=None)})()
        server._execution_runtime = lambda: runtime
        server._trigger_before_chat_request_hook = AsyncMock()
        socket = Socket()
        resume = AgentRequest(request_id="resume-1", channel_id="web", session_id=session_id, user_id="user-1",
                              req_method=ReqMethod.CHAT_SEND, is_stream=is_stream, metadata={"ws_id": "ws-1"},
                              params={"source": "client_tool_interrupt", "answers": _ANSWERS})
        await server.dispatch_parsed_request(socket, resume, asyncio.Lock())
        return socket.sent, agent

    sent, agent = asyncio.run(scenario())

    assert len(agent.delivered) == 1
    assert not [m for m in sent if (m.get("body") or {}).get("details", {}).get("code")]
    assert "call-c" in json.dumps(sent)
