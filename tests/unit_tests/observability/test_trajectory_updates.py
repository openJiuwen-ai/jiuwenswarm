# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for committed trajectory update hint fan-out."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
from jiuwenswarm.observability.models import CommittedTraceUpdate
from jiuwenswarm.observability.updates import TrajectoryUpdateBroker

test_logger = logging.getLogger("tests.trajectory_updates")


@dataclass(frozen=True)
class _RoutingKey:
    session_id: str


class _WebSocket:
    closed = False


def test_update_broker_isolates_listeners_and_unregisters() -> None:
    broker = TrajectoryUpdateBroker()
    received: list[tuple[CommittedTraceUpdate, ...]] = []

    def _failing(_updates: tuple[CommittedTraceUpdate, ...]) -> None:
        raise RuntimeError("injected listener failure")

    received_listener = received.append
    broker.register(_failing)
    broker.register(received_listener)
    updates = (
        CommittedTraceUpdate(
            session_id="session-1",
            trace_id="1" * 32,
            revision=7,
            store_epoch="epoch-1",
            lifecycle="running",
        ),
    )

    broker.publish(updates)
    broker.unregister(received_listener)
    broker.publish(updates)

    assert received == [updates]
    test_logger.info("commit hint listener failures stayed isolated")


@pytest.mark.asyncio
async def test_webchannel_routes_trace_update_to_matching_session_only() -> None:
    channel = WebChannel.__new__(WebChannel)
    first_ws = _WebSocket()
    other_ws = _WebSocket()
    channel._clients_by_key = {
        _RoutingKey(session_id="session-1"): [first_ws],
        _RoutingKey(session_id="session-2"): [other_ws],
    }
    sent: list[tuple[object, str, dict[str, object]]] = []

    async def _send_event(ws, event, payload) -> None:
        sent.append((ws, event, payload))

    channel.send_event = _send_event
    update = CommittedTraceUpdate(
        session_id="session-1",
        trace_id="2" * 32,
        revision=9,
        store_epoch="epoch-2",
        lifecycle="final",
    )

    await channel._send_trajectory_updates((update,))

    assert len(sent) == 1
    assert sent[0][0] is first_ws
    assert sent[0][1] == "trace.updated"
    assert sent[0][2] == {
        "session_id": "session-1",
        "trace_id": "2" * 32,
        "revision": 9,
        "store_epoch": "epoch-2",
        "lifecycle": "final",
    }
    test_logger.info("trace update hint stayed scoped to its session")


@pytest.mark.asyncio
async def test_webchannel_coalesces_running_backlog_into_latest_final_hint(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.web_connect."
        "_TRAJECTORY_HINT_COALESCE_SECONDS",
        0,
    )
    channel = WebChannel.__new__(WebChannel)
    channel._trajectory_pending_updates = {}
    channel._trajectory_send_task = None
    sent: list[tuple[CommittedTraceUpdate, ...]] = []

    async def _send(updates) -> None:
        sent.append(tuple(updates))

    channel._send_trajectory_updates = _send
    running = CommittedTraceUpdate(
        session_id="session-1",
        trace_id="3" * 32,
        revision=100,
        lifecycle="running",
    )
    stale = CommittedTraceUpdate(
        session_id="session-1",
        trace_id="3" * 32,
        revision=99,
        lifecycle="running",
    )
    final = CommittedTraceUpdate(
        session_id="session-1",
        trace_id="3" * 32,
        revision=101,
        lifecycle="final",
    )

    channel.schedule_trajectory_updates((running,))
    channel.schedule_trajectory_updates((stale, final))
    task = channel._trajectory_send_task
    assert task is not None
    await task

    assert sent == [(final,)]
    assert channel._trajectory_pending_updates == {}
    assert channel._trajectory_send_task is None
    test_logger.info("latest final hint absorbed the queued running backlog")
