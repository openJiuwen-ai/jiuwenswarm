# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for Front-owned business push delivery."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.wire_codec import is_e2a_response_wire_dict
from jiuwenswarm.observability.gateway_hints import TrajectoryGatewayHintBridge
from jiuwenswarm.observability.models import CommittedTraceUpdate
from jiuwenswarm.server.front.server import AgentServerFront

test_logger = logging.getLogger("tests.front_push")


class _Socket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.sent = asyncio.Event()

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))
        self.sent.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", [None, "custom.event"])
async def test_front_push_encodes_business_message(response_kind: str | None) -> None:
    front = AgentServerFront()
    socket = _Socket()
    front.forwarder.attach(socket, asyncio.Lock())
    payload = {"event_type": "trace.updated", "revision": 6, "frame_seq": 123}
    message: dict[str, Any] = {
        "request_id": "trajectory:session-1:trace:6:123",
        "channel_id": "web",
        "session_id": "session-1",
        "payload": payload,
        "metadata": {"app_id": "app-1"},
        "is_complete": False,
    }
    if response_kind is not None:
        message.update(response_kind=response_kind, body=payload)

    assert await front.send_push(message) is True
    wire = socket.frames[0]
    assert is_e2a_response_wire_dict(wire)
    assert wire["metadata"][E2A_WIRE_SERVER_PUSH_KEY] is True
    assert wire["metadata"]["app_id"] == "app-1"
    assert wire["request_id"] == message["request_id"]
    assert wire["session_id"] == "session-1"
    assert wire["channel"] == "web"
    assert "metadata" in message and E2A_WIRE_SERVER_PUSH_KEY not in message["metadata"]
    if response_kind is not None:
        assert wire["response_kind"] == response_kind
        assert wire["body"] == payload
    else:
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk

        assert parse_agent_server_wire_chunk(wire).payload == payload
    test_logger.info("Front encoded the push with its routing and business payload intact")


@pytest.mark.asyncio
async def test_front_push_reports_missing_connection() -> None:
    front = AgentServerFront()
    assert await front.send_push({"request_id": "push-1", "payload": {}}) is False
    test_logger.info("Missing connection reports failure to the push producer")


@pytest.mark.asyncio
async def test_front_push_preserves_original_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    front = AgentServerFront()
    socket = _Socket()
    front.forwarder.attach(socket, asyncio.Lock())

    async def _fallback_send(ws: Any, wire: dict[str, Any]) -> bool:
        assert ws is socket
        assert wire["metadata"][E2A_WIRE_SERVER_PUSH_KEY] is True
        return False

    monkeypatch.setattr("jiuwenswarm.server.front.event_forwarder.send_wire_payload", _fallback_send)
    assert await front.send_push({"request_id": "push-1", "payload": {}}) is False
    test_logger.info("A fallback frame does not count as successful original push delivery")


@pytest.mark.asyncio
async def test_front_push_reports_socket_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    front = AgentServerFront()
    socket = _Socket()
    front.forwarder.attach(socket, asyncio.Lock())

    async def _closed_send(data: str) -> None:
        raise ConnectionError("Gateway disconnected")

    monkeypatch.setattr(socket, "send", _closed_send)
    assert await front.send_push({"request_id": "push-1", "payload": {}}) is False
    test_logger.info("Socket failure reports False for producer retry")


@pytest.mark.asyncio
async def test_trajectory_bridge_retries_through_front_after_reconnect() -> None:
    front = AgentServerFront()
    bridge = TrajectoryGatewayHintBridge()
    first_attempt = asyncio.Event()
    socket = _Socket()

    async def _send(message: dict[str, Any]) -> bool:
        result = await front.send_push(message)
        first_attempt.set()
        return result

    bridge.bind(asyncio.get_running_loop(), _send)
    try:
        bridge.publish((CommittedTraceUpdate("session-1", "a" * 32, 6, frame_seq=123),))
        await asyncio.wait_for(first_attempt.wait(), timeout=2)
        front.forwarder.attach(socket, asyncio.Lock())
        await asyncio.wait_for(socket.sent.wait(), timeout=2)
        assert len(socket.frames) == 1
        assert socket.frames[0]["metadata"][E2A_WIRE_SERVER_PUSH_KEY] is True
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk

        payload = parse_agent_server_wire_chunk(socket.frames[0]).payload
        assert payload["event_type"] == "trace.updated"
        assert payload["revision"] == 6
        assert payload["frame_seq"] == 123
    finally:
        await bridge.unbind()
    test_logger.info("Front reconnect delivered the retained trajectory watermark")


def test_runtime_send_push_without_gateway_warning_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server import agent_ws_server
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    now = [100.0]
    monkeypatch.setattr(agent_ws_server.time, "monotonic", lambda: now[0])
    messages: list[str] = []

    class _RecordingLogger:
        def warning(self, msg: str, *args: object) -> None:
            messages.append(msg % args if args else msg)

        info = warning

    monkeypatch.setattr(agent_ws_server, "logger", _RecordingLogger())
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)

    for _ in range(5):
        server._warn_push_without_gateway()  # pylint: disable=protected-access
    now[0] += agent_ws_server._PUSH_NO_GATEWAY_WARN_INTERVAL_S + 1  # pylint: disable=protected-access
    server._warn_push_without_gateway()  # pylint: disable=protected-access
    server._warn_push_without_gateway()  # pylint: disable=protected-access
    server._reset_push_no_gateway_warning()  # pylint: disable=protected-access

    warnings = [message for message in messages if "无活跃 Gateway 连接" in message]
    assert len(warnings) == 2
    assert "另有 4 条" in warnings[1]
    assert any("此前 1 条" in message for message in messages)
    test_logger.info("missing-Gateway push warnings were summarized per window")
