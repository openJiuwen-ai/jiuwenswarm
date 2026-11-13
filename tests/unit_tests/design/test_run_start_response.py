"""`designer.run.start` must acknowledge the snapshot THIS trigger persisted.

Issue #5326: regenerating a node from the canvas reported the previous
lifelong-run record (or a synthesized placeholder), so the client could not see
the new round and the second regeneration was never adopted. These tests pin
the acknowledgement helper in isolation: first persisted update wins, a
start-time crash becomes a prompt error, and a silent trigger falls back to the
legacy accept-and-trigger contract instead of hanging the RPC.
"""

import asyncio

import pytest

from jiuwenswarm.server.runtime.gateway_adapter.designer_adapter import (
    _launch_run_trigger_and_ack,
)

PAYLOAD = {
    "graph_id": "g1",
    "graph": {"graph_id": "g1"},
    "node_ids": ["leader"],
    "cascade": False,
}


class _FakeCoordinator:
    def __init__(self, updates=None, *, crash=None, delay=0.0, hold=0.0):
        self._updates = list(updates or [])
        self._crash = crash
        self._delay = delay
        self._hold = hold
        self.started = False

    async def start(self, graph, *, node_ids=None, cascade=False, on_update=None):
        del graph, node_ids, cascade
        self.started = True
        if self._crash:
            raise RuntimeError(self._crash)
        for update in self._updates:
            if self._delay:
                await asyncio.sleep(self._delay)
            if on_update is not None:
                on_update(update)
        if self._hold:
            await asyncio.sleep(self._hold)


@pytest.mark.asyncio
async def test_ack_returns_first_persisted_snapshot():
    pushed = []
    first = {"run_id": "r2", "graph_id": "g1", "status": "running", "node_states": {"leader": {"version": 5}}}
    later = {"run_id": "r2", "graph_id": "g1", "status": "completed", "node_states": {"leader": {"version": 5}}}
    coordinator = _FakeCoordinator([first, later], delay=0.0)

    snapshot, error = await _launch_run_trigger_and_ack(
        coordinator, PAYLOAD, pushed.append, timeout_seconds=2, failure_grace_seconds=0.01
    )

    assert error is None
    assert snapshot is not None
    assert snapshot["run_id"] == "r2"
    assert snapshot["node_states"]["leader"]["version"] == 5
    # every update still reaches the client push channel
    assert [item["status"] for item in pushed] == ["running", "completed"]


@pytest.mark.asyncio
async def test_ack_returns_prompt_error_when_start_crashes_before_snapshot():
    coordinator = _FakeCoordinator([], crash="graph has no runnable node")

    snapshot, error = await _launch_run_trigger_and_ack(
        coordinator, PAYLOAD, None, timeout_seconds=2, failure_grace_seconds=0.01
    )

    assert snapshot is None
    assert error == "graph has no runnable node"


@pytest.mark.asyncio
async def test_ack_reports_immediate_terminal_failure_after_snapshot():
    running = {"run_id": "r2", "graph_id": "g1", "status": "running"}
    failed = {
        "run_id": "r2",
        "graph_id": "g1",
        "status": "failed",
        "error": "compile rejected the graph",
    }
    coordinator = _FakeCoordinator([running, failed])

    snapshot, error = await _launch_run_trigger_and_ack(
        coordinator, PAYLOAD, None, timeout_seconds=2, failure_grace_seconds=0.5
    )

    assert snapshot is None
    assert error == "compile rejected the graph"


@pytest.mark.asyncio
async def test_ack_falls_back_to_placeholder_when_trigger_stays_silent():
    coordinator = _FakeCoordinator([], hold=5)

    snapshot, error = await _launch_run_trigger_and_ack(
        coordinator, PAYLOAD, None, timeout_seconds=0.05, failure_grace_seconds=0.01
    )

    assert snapshot is None
    assert error is None


@pytest.mark.asyncio
async def test_ack_does_not_cancel_the_live_run_after_the_grace_window():
    running = {"run_id": "r2", "graph_id": "g1", "status": "running"}
    coordinator = _FakeCoordinator([running], hold=0.3)

    snapshot, error = await _launch_run_trigger_and_ack(
        coordinator, PAYLOAD, None, timeout_seconds=2, failure_grace_seconds=0.05
    )

    assert error is None
    assert snapshot is not None
    # the background generation keeps running after the acknowledgement
    await asyncio.sleep(0.35)
    assert coordinator.started
