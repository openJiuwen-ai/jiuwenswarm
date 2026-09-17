# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow checkpoint persistence unit tests."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from jiuwenswarm.agents.harness.team.handlers.workflow_state import (
    WorkflowRunState,
    WorkflowProgress,
)
from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler


_DEFAULT_RUN_ID = "wf_testrun00001"


def _make_progress(kind: str, **kwargs: Any) -> WorkflowProgress:
    if "run_id" not in kwargs:
        kwargs["run_id"] = _DEFAULT_RUN_ID
    return WorkflowProgress(kind=kind, **kwargs)


class _FakeTeamMonitor:
    """Minimal TeamMonitor stand-in for unit tests."""

    def __init__(self) -> None:
        self._queue: list[object] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self._queue.append(None)

    async def workflow_events(self) -> AsyncIterator[object]:
        while self._queue:
            event = self._queue.pop(0)
            if event is None:
                break
            yield event

    def put_event(self, event: object) -> None:
        self._queue.append(event)

    async def drain(self) -> None:
        """Wait until all injected events have been consumed."""
        while self._queue:
            await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Round-trip tests for WorkflowRunState model_dump / model_validate
# ---------------------------------------------------------------------------


class TestWorkflowCheckpointRoundTrip:

    @staticmethod
    def test_model_dump_and_restore_preserves_state():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a", prompt="prompt"))

        runs_data = {state.id: state.model_dump()}
        restored = {
            k: WorkflowRunState.model_validate(v)
            for k, v in runs_data.items()
        }

        assert len(restored) == 1
        r = restored[state.id]
        assert r.id == state.id
        assert r.name == "test"
        assert r.status == "running"
        assert len(r.phases) == 1
        assert r.phases[0].name == "Phase 1"

    @staticmethod
    def test_terminal_state_survives_round_trip():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("workflow_completed", text="done"))

        runs_data = {state.id: state.model_dump()}
        restored = {
            k: WorkflowRunState.model_validate(v)
            for k, v in runs_data.items()
        }

        r = restored[state.id]
        assert r.status == "completed"
        assert r.is_terminal is True


# ---------------------------------------------------------------------------
# WorkflowMonitorHandler initial_runs (interrupt recovery)
# ---------------------------------------------------------------------------


class TestWorkflowMonitorHandlerInitialRuns:

    @staticmethod
    def test_handler_starts_with_initial_runs() -> None:
        """Handler initialised with restored runs preserves them."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="restored-flow"))
        state.apply(_make_progress("agent_started", phase="Planning", label="agent-a"))

        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={state.id: state},
        )

        snapshot = handler.get_workflow_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["name"] == "restored-flow"
        assert snapshot[0]["status"] == "running"
        assert len(snapshot[0]["phases"]) == 1

    @staticmethod
    def test_handler_without_initial_runs_starts_empty() -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        assert handler.get_workflow_snapshot() == []

    @pytest.mark.anyio
    async def test_initial_runs_plus_new_events_merge(self) -> None:
        """Restored runs + new events from stream produce combined snapshot."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="old-flow"))
        state.apply(_make_progress("agent_started", phase="Planning", label="agent-a"))

        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={state.id: state},
        )
        await handler.start()

        # Push a new agent_started event for the existing run (phase via agent event)
        from types import SimpleNamespace
        new_progress = WorkflowProgress(
            kind="agent_started",
            run_id=state.id,
            phase="NewPhase",
            label="agent-b",
        )
        monitor.put_event(SimpleNamespace(
            event_type=SimpleNamespace(value="workflow_progress"),
            sender_id="swarmflow",
            payload=new_progress,
            get_payload=lambda: new_progress,
        ))
        await monitor.drain()
        await monitor.stop()
        await handler.stop()

        snapshot = handler.get_workflow_snapshot()
        assert len(snapshot) == 1
        phase_names = {p["name"] for p in snapshot[0]["phases"]}
        assert "Planning" in phase_names
        assert "NewPhase" in phase_names