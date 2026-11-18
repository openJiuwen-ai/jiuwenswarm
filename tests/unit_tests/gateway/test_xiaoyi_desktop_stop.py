"""A phone stop has the same runtime effect as the desktop stop button."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.gateway.message_handler import external_conv_session
from jiuwenswarm.gateway.routing.keys import XiaoyiDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget


@pytest.fixture(autouse=True)
def _avoid_external_log_directory():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _channel() -> tuple[XiaoyiChannel, list[Message], list[tuple[str, str, dict]]]:
    channel = XiaoyiChannel(XiaoyiChannelConfig(agent_id="agent-1"), RobotMessageRouter())
    received: list[Message] = []
    responses: list[tuple[str, str, dict]] = []
    channel.on_message(received.append)
    channel._ws_connections["ws"] = object()

    async def capture(session_id: str, task_id: str, response: dict, url_key: str) -> None:
        responses.append((session_id, task_id, response))

    channel._send_agent_response = capture  # type: ignore[assignment]
    return channel, received, responses


@pytest.mark.asyncio
async def test_phone_stop_with_top_level_task_id_reaches_gateway() -> None:
    channel, received, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")

    await channel._handle_tasks_cancel({
        "jsonrpc": "2.0",
        "id": "task-1",
        "method": "tasks/cancel",
        "sessionId": "conversation-1",
    })

    assert len(received) == 1
    stop = received[0]
    assert stop.req_method == ReqMethod.CHAT_CANCEL
    assert stop.session_id == "conversation-1"
    assert stop.params["request_id"] == "task-1"
    assert stop.params["desktop_stop"] is True
    assert responses[0][0:2] == ("conversation-1", "task-1")
    assert responses[0][2]["result"]["status"]["state"] == "canceled"
    # 取消即消费：先发「已停止」正文（canceled 终态会被上游自动重发该消息，
    # 与后续轮次互相残杀；正文走 artifact 而非 status text，否则桌面镜像
    # 渲染「本轮无响应」），再发 completed 终态。
    close_text = responses[1][2]["result"]
    assert responses[1][0:2] == ("conversation-1", "task-1")
    assert close_text["kind"] == "artifact-update"
    assert close_text["final"] is True
    assert close_text["artifact"]["parts"][0]["text"] == "已停止"
    terminal = responses[2][2]["result"]
    assert responses[2][0:2] == ("conversation-1", "task-1")
    assert terminal["kind"] == "status-update"
    assert terminal["final"] is True
    assert terminal["status"]["state"] == "completed"
    assert ("conversation-1", "task-1") not in channel._active_tasks


@pytest.mark.asyncio
async def test_stale_phone_stop_does_not_interrupt_newer_task() -> None:
    channel, received, responses = _channel()
    channel._mark_session_active("conversation-1", "task-2")

    await channel._handle_tasks_cancel({
        "jsonrpc": "2.0",
        "id": "task-1",
        "method": "tasks/cancel",
        "sessionId": "conversation-1",
    })

    assert received == []
    assert ("conversation-1", "task-2") in channel._active_tasks
    assert responses[0][1] == "task-1"


@pytest.mark.asyncio
async def test_phone_stop_without_task_id_reports_invalid_request() -> None:
    channel, received, responses = _channel()
    channel._mark_session_active("conversation-1", "task-2")

    await channel._handle_tasks_cancel({
        "jsonrpc": "2.0",
        "method": "tasks/cancel",
        "sessionId": "conversation-1",
    })

    assert received == []
    assert ("conversation-1", "task-2") in channel._active_tasks
    assert responses[0][2]["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_failed_stop_dispatch_keeps_task_output_visible() -> None:
    channel, _, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")

    def fail_dispatch(_message: Message) -> None:
        raise RuntimeError("gateway unavailable")

    channel.on_message(fail_dispatch)
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await channel._handle_tasks_cancel({
            "jsonrpc": "2.0", "id": "task-1", "method": "tasks/cancel",
            "sessionId": "conversation-1",
        })

    assert responses == []
    assert ("conversation-1", "task-1") in channel._active_tasks
    assert ("conversation-1", "task-1") not in channel._canceled_platform_tasks


@pytest.mark.asyncio
async def test_phone_stop_clears_old_mobile_keepalive_and_buffered_text() -> None:
    channel, _, _ = _channel()
    channel._mark_session_active("conversation-1", "task-1")
    # The push map uses the physical reply address, not the logical conversation.
    channel._active_push_sessions["agent-1"] = (
        "physical-session", "task-1", "push-1", 1.0,
    )
    channel._ws_flush_buffers["task-1"] = "late text"
    flush = asyncio.create_task(asyncio.sleep(3600))
    channel._ws_flush_tasks["task-1"] = flush
    try:
        await channel._handle_tasks_cancel({
            "jsonrpc": "2.0",
            "id": "task-1",
            "method": "tasks/cancel",
            "sessionId": "conversation-1",
            "agentId": "agent-1",
        })

        assert "agent-1" not in channel._active_push_sessions
        assert "task-1" not in channel._ws_flush_buffers
        assert "task-1" not in channel._ws_flush_tasks
        await asyncio.sleep(0)
        assert flush.cancelled()
    finally:
        flush.cancel()
        await asyncio.gather(flush, return_exceptions=True)


@pytest.mark.asyncio
async def test_phone_stop_during_keepalive_does_not_resurrect_old_task() -> None:
    channel, _, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")
    channel._session_task_map["task-1"] = "conversation-1"
    channel._active_push_sessions["agent-1"] = (
        "physical-session", "task-1", "push-1", time.time(),
    )
    channel._team_ws_keepalive_interval = 0.01
    channel._running = True
    states: list[str] = []
    original_send = channel._send_status_update_with_state

    async def send_during_cancel(
        task_id: str, session_id: str, message: str, state: str, url_key: str,
        extra_part: dict | None = None,
    ) -> None:
        states.append(state)
        if state == "working":
            await channel._handle_tasks_cancel({
                "jsonrpc": "2.0", "id": "task-1", "method": "tasks/cancel",
                "sessionId": "conversation-1", "agentId": "agent-1",
            })
        else:
            await original_send(task_id, session_id, message, state, url_key, extra_part)

    channel._send_status_update_with_state = send_during_cancel  # type: ignore[assignment]
    keepalive = asyncio.create_task(channel._ws_keepalive_loop())
    try:
        await asyncio.sleep(0.04)
    finally:
        channel._running = False
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

    assert states == ["working", "completed"]
    assert "agent-1" not in channel._active_push_sessions
    assert any(r[2].get("result", {}).get("kind") == "status-update" for r in responses)


@pytest.mark.asyncio
async def test_keepalive_skips_task_while_cancel_is_waiting_for_gateway() -> None:
    channel, _, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")
    channel._session_task_map["task-1"] = "conversation-1"
    channel._active_push_sessions["agent-1"] = (
        "physical-session", "task-1", "push-1", time.time(),
    )
    channel._canceled_platform_tasks[("conversation-1", "task-1")] = time.time()
    channel._team_ws_keepalive_interval = 0.01
    channel._running = True
    keepalive = asyncio.create_task(channel._ws_keepalive_loop())
    try:
        await asyncio.sleep(0.025)
    finally:
        channel._running = False
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

    assert responses == []


def _outbound(task_id: str, event_type: EventType, content: str) -> Message:
    return Message(
        id=task_id,
        type="event",
        channel_id="xiaoyi",
        session_id="conversation-1",
        params={},
        timestamp=1.0,
        ok=True,
        payload={"event_type": event_type.value, "content": content},
        event_type=event_type,
        metadata={"xiaoyi_session_id": "conversation-1", "xiaoyi_task_id": task_id},
    )


@pytest.mark.asyncio
async def test_cancelled_task_drops_queued_text_but_new_task_can_send() -> None:
    channel, _, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")
    await channel._handle_tasks_cancel({
        "jsonrpc": "2.0", "id": "task-1", "method": "tasks/cancel",
        "sessionId": "conversation-1",
    })
    responses.clear()

    await channel.send(_outbound("task-1", EventType.CHAT_REASONING, "queued text"))
    assert responses == []

    channel._mark_session_active("conversation-1", "task-2")
    await channel.send(_outbound("task-2", EventType.CHAT_REASONING, "new text"))
    assert responses
    assert all(task_id == "task-2" for _, task_id, _ in responses)


@pytest.mark.asyncio
async def test_cancelled_team_task_does_not_fall_back_to_push() -> None:
    channel, _, responses = _channel()
    channel._mark_session_active("conversation-1", "task-1")
    channel._active_push_sessions["agent-1"] = (
        "physical-session", "task-1", "push-1", 1.0,
    )
    pushes: list[str] = []

    async def capture_push(agent_id: str, push_id: str, content: str) -> None:
        pushes.append(content)

    channel._send_push_to_user = capture_push  # type: ignore[assignment]
    await channel._handle_tasks_cancel({
        "jsonrpc": "2.0", "id": "task-1", "method": "tasks/cancel",
        "sessionId": "conversation-1", "agentId": "agent-1",
    })
    responses.clear()
    target = RoutingTarget(
        intent="godview",
        delivery=XiaoyiDeliveryTarget(
            agent_id="agent-1", push_id="push-1",
            xiaoyi_session_id="physical-session", conversation_id="conversation-1",
        ),
    )

    await channel.send(
        _outbound("task-1", EventType.CHAT_FINAL, "queued team output"),
        routing_target=target,
    )
    assert pushes == []
    assert responses == []

    # A new phone turn must not receive the old queued Team output under its ID.
    channel._mark_session_active("conversation-1", "task-2")
    channel._active_push_sessions["agent-1"] = (
        "physical-session-2", "task-2", "push-2", time.time(),
    )
    await channel.send(
        _outbound("task-1", EventType.CHAT_FINAL, "old queued output"),
        routing_target=target,
    )
    assert responses == []
    await channel.send(
        _outbound("task-2", EventType.CHAT_FINAL, "new output"),
        routing_target=target,
    )
    assert responses
    assert all(task_id == "task-2" for _, task_id, _ in responses)

    # A resumed Team runtime may still emit under its original stream ID.
    resumed = replace(
        _outbound("task-1", EventType.CHAT_FINAL, "resumed output"),
        timestamp=time.time() + 1,
    )
    await channel.send(resumed, routing_target=target)
    assert responses[-1][1] == "task-2"


class _AgentClient:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.sent = asyncio.Event()

    async def send_request(self, env: object) -> SimpleNamespace:
        self.requests.append(env)
        self.sent.set()
        return SimpleNamespace(
            request_id="interrupt-1",
            channel_id="xiaoyi",
            ok=True,
            payload={"event_type": "chat.interrupt_result", "success": True},
            metadata=None,
        )

    async def send_request_stream(self, env: object):
        if False:
            yield env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_mode", "expected_intent", "expect_cancelled"),
    [("agent", "cancel", True), ("team", "pause", False)],
)
async def test_phone_stop_matches_desktop_runtime_action(
    stream_mode: str, expected_intent: str, expect_cancelled: bool,
) -> None:
    MessageHandler._instance = None
    client = _AgentClient()
    handler = MessageHandler(client)
    handler._external_session_aliases[("xiaoyi", "conversation-1")] = "session-1"
    handler._running = True

    async def running() -> None:
        await asyncio.Event().wait()

    stream = asyncio.create_task(running())
    handler._stream_tasks["task-1"] = stream
    handler._stream_sessions["task-1"] = "session-1"
    handler._stream_channels["task-1"] = "xiaoyi"
    handler._stream_modes["task-1"] = stream_mode
    handler._stream_emits_processing_status["task-1"] = False
    forward = asyncio.create_task(handler._forward_loop())
    try:
        await handler.handle_message(Message(
            id="task-1:stop",
            type="req",
            channel_id="xiaoyi",
            session_id="conversation-1",
            params={
                "desktop_stop": True,
                "intent": "cancel",
                "session_id": "conversation-1",
                "request_id": "task-1",
            },
            timestamp=0.0,
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
        ))
        await asyncio.wait_for(client.sent.wait(), timeout=2)
        assert client.requests[0].params["intent"] == expected_intent
        if stream_mode == "team":
            assert client.requests[0].params["mode"] == "team"
        assert stream.cancelled() is expect_cancelled
    finally:
        forward.cancel()
        stream.cancel()
        await asyncio.gather(forward, stream, return_exceptions=True)
        handler._running = False
        MessageHandler._instance = None


@pytest.mark.asyncio
async def test_phone_stop_uses_locked_team_mode_over_stream_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        external_conv_session,
        "read_locked_session_mode",
        lambda session_id: "team" if session_id == "desktop-team-1" else "",
    )
    MessageHandler._instance = None
    client = _AgentClient()
    handler = MessageHandler(client)
    alias = ("xiaoyi", "conversation-1")
    handler._external_session_aliases[alias] = "desktop-team-1"
    handler._external_session_reused.add(alias)
    handler._running = True
    async def running() -> None:
        await asyncio.Event().wait()

    stream = asyncio.create_task(running())
    handler._stream_tasks["task-1"] = stream
    handler._stream_sessions["task-1"] = "desktop-team-1"
    handler._stream_channels["task-1"] = "xiaoyi"
    # Reused desktop sessions can have no injected mode; gateway bookkeeping
    # records its fallback "plan" even though the locked runtime is Team.
    handler._stream_modes["task-1"] = "plan"
    handler._stream_emits_processing_status["task-1"] = False
    forward = asyncio.create_task(handler._forward_loop())
    try:
        await handler.handle_message(Message(
            id="task-1:stop",
            type="req",
            channel_id="xiaoyi",
            session_id="conversation-1",
            params={
                "desktop_stop": True,
                "intent": "cancel",
                "session_id": "conversation-1",
                "request_id": "task-1",
            },
            timestamp=0.0,
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
        ))
        await asyncio.wait_for(client.sent.wait(), timeout=2)
        assert client.requests[0].params["intent"] == "pause"
        assert client.requests[0].params["mode"] == "team"
        assert client.requests[0].session_id == "desktop-team-1"
        assert not stream.cancelled()
    finally:
        forward.cancel()
        stream.cancel()
        await asyncio.gather(forward, stream, return_exceptions=True)
        handler._running = False
        MessageHandler._instance = None
