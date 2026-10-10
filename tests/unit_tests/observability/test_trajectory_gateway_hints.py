# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the AgentServer-to-Gateway trajectory hint bridge."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
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
            CommittedTraceUpdate(
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
