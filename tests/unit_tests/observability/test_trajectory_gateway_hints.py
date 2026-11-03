# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the AgentServer-to-Gateway trajectory hint bridge."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway.channel_manager.web.trace_update import (
    CommittedTraceUpdate as GatewayCommittedTraceUpdate,
)
from gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.observability.gateway_hints import TrajectoryGatewayHintBridge
from jiuwenswarm.observability.models import CommittedTraceUpdate
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

test_logger = logging.getLogger("tests.trajectory_gateway_hints")


@pytest.mark.asyncio
async def test_bridge_catches_up_revision_committed_while_send_is_in_flight() -> None:
    bridge = TrajectoryGatewayHintBridge()
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent: list[dict[str, object]] = []

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)
        if len(sent) == 1:
            first_send_started.set()
            await release_first_send.wait()

    loop = asyncio.get_running_loop()
    bridge.bind(loop, _send)
    bridge.publish(
        (
            CommittedTraceUpdate("session-1", "a" * 32, 10, lifecycle="running"),
            CommittedTraceUpdate("session-1", "a" * 32, 11, lifecycle="running"),
        )
    )
    await first_send_started.wait()
    bridge.publish(
        (
            CommittedTraceUpdate("session-1", "a" * 32, 12, lifecycle="final"),
        )
    )
    release_first_send.set()

    for _attempt in range(20):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    await bridge.unbind()

    assert [message["payload"]["revision"] for message in sent] == [11, 12]
    assert sent[1]["payload"]["lifecycle"] == "final"
    test_logger.info("in-flight send caught up the terminal revision")


@pytest.mark.asyncio
async def test_gateway_routes_cross_process_hint_into_webchannel_coalescer() -> None:
    handler = object.__new__(MessageHandler)
    scheduled: list[tuple[CommittedTraceUpdate, ...]] = []
    web_channel = SimpleNamespace(
        schedule_trajectory_updates=lambda updates: scheduled.append(tuple(updates))
    )
    handler._resolve_web_channel = lambda: web_channel
    handler._stream_sessions = {}
    wire = build_server_push_wire(
        {
            "request_id": "trajectory:session-1:trace:21",
            "channel_id": "web",
            "session_id": "session-1",
            "is_complete": False,
            "payload": {
            "event_type": "trace.updated",
            "session_id": "session-1",
            "trace_id": "b" * 32,
            "revision": 21,
            "store_epoch": "epoch-1",
            "lifecycle": "running",
            },
        }
    )

    await handler._handle_agent_server_push(wire)

    assert scheduled == [
        (
            GatewayCommittedTraceUpdate(
                session_id="session-1",
                trace_id="b" * 32,
                revision=21,
                store_epoch="epoch-1",
                lifecycle="running",
            ),
        )
    ]
    test_logger.info("Gateway handed the cross-process hint to WebChannel")


@pytest.mark.asyncio
async def test_bridge_preserves_first_hints_for_distinct_subagent_traces() -> None:
    bridge = TrajectoryGatewayHintBridge()
    sent: list[dict[str, object]] = []

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    bridge.bind(asyncio.get_running_loop(), _send)
    bridge.publish(
        (
            CommittedTraceUpdate("session-1", "c" * 32, 1, lifecycle="running"),
            CommittedTraceUpdate("session-1", "d" * 32, 2, lifecycle="running"),
        )
    )
    for _attempt in range(20):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    await bridge.unbind()

    assert {message["payload"]["trace_id"] for message in sent} == {
        "c" * 32,
        "d" * 32,
    }
    test_logger.info("first updates for distinct execution traces were both delivered")


@pytest.mark.asyncio
async def test_bridge_retries_hint_rejected_during_gateway_disconnect() -> None:
    bridge = TrajectoryGatewayHintBridge()
    attempts: list[dict[str, object]] = []
    delivered = asyncio.Event()

    async def _send(message: dict[str, object]) -> bool:
        attempts.append(message)
        if len(attempts) == 1:
            return False
        delivered.set()
        return True

    bridge.bind(asyncio.get_running_loop(), _send)
    bridge.publish(
        (
            CommittedTraceUpdate("session-1", "e" * 32, 31, lifecycle="final"),
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=2)
    await bridge.unbind()

    assert len(attempts) == 2
    assert attempts[1]["payload"]["revision"] == 31
    test_logger.info("temporary Gateway disconnect retained and retried the watermark")


class _RecordingAsyncio:
    """Proxy ``asyncio`` that records retry waits instead of sleeping."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)

    async def wait_for(self, awaitable: object, timeout: float) -> None:
        self.timeouts.append(timeout)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_bridge_backs_off_exponentially_and_resets_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.observability import gateway_hints

    recorder = _RecordingAsyncio()
    monkeypatch.setattr(gateway_hints, "asyncio", recorder)
    bridge = TrajectoryGatewayHintBridge()
    results = [False] * 8 + [True, False, True]
    attempts: list[int] = []
    finished = asyncio.Event()

    async def _send(message: dict[str, object]) -> bool:
        attempts.append(int(message["payload"]["revision"]))
        result = results[len(attempts) - 1]
        if len(attempts) in (9, len(results)):
            finished.set()
        return result

    bridge.bind(asyncio.get_running_loop(), _send)
    bridge.publish((CommittedTraceUpdate("session-1", "f" * 32, 40),))
    await asyncio.wait_for(finished.wait(), timeout=2)
    finished.clear()
    assert recorder.timeouts == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]

    bridge.publish((CommittedTraceUpdate("session-1", "f" * 32, 41),))
    await asyncio.wait_for(finished.wait(), timeout=2)
    await bridge.unbind()

    assert recorder.timeouts[8:] == [0.1]
    assert attempts[-1] == 41
    test_logger.info("retry delay doubled up to the cap and reset after delivery")


@pytest.mark.asyncio
async def test_bridge_notify_available_retries_without_waiting_backoff() -> None:
    bridge = TrajectoryGatewayHintBridge()
    attempts: list[dict[str, object]] = []
    first_rejected = asyncio.Event()
    delivered = asyncio.Event()

    async def _send(message: dict[str, object]) -> bool:
        attempts.append(message)
        if len(attempts) == 1:
            first_rejected.set()
            return False
        delivered.set()
        return True

    bridge.bind(asyncio.get_running_loop(), _send)
    bridge._retry_delay = 60.0  # pylint: disable=protected-access
    bridge.publish((CommittedTraceUpdate("session-1", "9" * 32, 50),))
    await asyncio.wait_for(first_rejected.wait(), timeout=2)
    await asyncio.sleep(0)
    bridge.notify_available()
    await asyncio.wait_for(delivered.wait(), timeout=2)
    await bridge.unbind()

    assert len(attempts) == 2
    assert attempts[1]["payload"]["revision"] == 50
    test_logger.info("Gateway reattach woke the pending retry immediately")
