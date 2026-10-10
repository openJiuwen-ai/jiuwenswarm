# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for WorkflowMonitorHandler — TeamMonitor-backed lifecycle + delta-queue handler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowProgress
from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler


# ---------------------------------------------------------------------------
# Fake TeamMonitor — controls what workflow_events() yields
# ---------------------------------------------------------------------------

class _FakeTeamMonitor:
    """Minimal TeamMonitor stand-in for unit tests.

    Feed raw events via put_event(); start()/stop() are recorded so tests
    can assert lifecycle ordering.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[object | None] = asyncio.Queue()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self._queue.put_nowait(None)  # sentinel terminates workflow_events()

    async def workflow_events(self) -> AsyncIterator[object]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    def put_event(self, event: object) -> None:
        """Inject a raw event for consumption by workflow_events()."""
        self._queue.put_nowait(event)

    async def drain(self) -> None:
        """Wait until all injected events have been yielded by workflow_events()."""
        while not self._queue.empty():
            await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Fake raw EventMessage carrying a WorkflowProgress payload
# ---------------------------------------------------------------------------

_DEFAULT_RUN_ID = "wf_testrun00001"


class _FakeRawEvent:
    """Simulates a raw EventMessage already filtered to workflow_progress."""

    def __init__(self, kind: str, run_id: str = _DEFAULT_RUN_ID, **kwargs: Any):
        self.event_type = SimpleNamespace(value="workflow_progress")
        self.sender_id = "swarmflow"
        self.payload = WorkflowProgress(kind=kind, run_id=run_id, **kwargs)

    def get_payload(self) -> WorkflowProgress:
        return self.payload


class _FakeAgentCoreRawEvent:
    """Simulates agent-core EventMessage with WorkflowProgressTeamEvent payload."""

    def __init__(self, kind: str, run_id: str = _DEFAULT_RUN_ID, **kwargs: Any) -> None:
        from openjiuwen.agent_teams.schema.events import EventMessage, WorkflowProgressTeamEvent
        from openjiuwen.agent_teams.workflow.engine.progress import PhasePlan as CorePhasePlan

        phases = kwargs.pop("phases", None)
        core_phases = None
        if phases is not None:
            core_phases = [
                CorePhasePlan(title=p.title, description=p.description)
                for p in phases
            ]
        self._message = EventMessage.from_event(
            WorkflowProgressTeamEvent(
                team_name="t",
                kind=kind,
                run_id=run_id,
                phases=core_phases,
                **kwargs,
            )
        )

    def get_payload(self) -> Any:
        return self._message.get_payload()


# ---------------------------------------------------------------------------
# Helper: start handler, inject events, then stop and drain
# ---------------------------------------------------------------------------

async def _run_handler_with_events(
    handler: WorkflowMonitorHandler,
    monitor: _FakeTeamMonitor,
    raw_events: list[_FakeRawEvent],
) -> list[dict[str, Any]]:
    """Start handler, inject events, stop, collect all workflow.updated dicts."""
    await handler.start()
    for ev in raw_events:
        monitor.put_event(ev)
    await monitor.drain()  # wait until all events have been consumed by collect task
    await handler.stop()

    results: list[dict[str, Any]] = []
    async for item in handler.events():
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# Init and property tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerInit:
    @staticmethod
    def test_init_defaults() -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        assert handler.session_id == "sess-1"
        assert handler.channel_id is None
        assert handler.is_running is False

    @staticmethod
    def test_init_with_channel_id() -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-2", channel_id="chan-1")
        assert handler.session_id == "sess-2"
        assert handler.channel_id == "chan-1"
        assert handler.is_running is False


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerLifecycle:
    @pytest.mark.anyio
    async def test_start_calls_monitor_start(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        await handler.start()
        assert monitor.started is True
        assert handler.is_running is True
        await handler.stop()

    @pytest.mark.anyio
    async def test_stop_calls_monitor_stop(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        await handler.start()
        await handler.stop()
        assert monitor.stopped is True
        assert handler.is_running is False

    @pytest.mark.anyio
    async def test_double_start_is_idempotent(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        await handler.start()
        await handler.start()
        assert handler.is_running is True
        await handler.stop()

    @pytest.mark.anyio
    async def test_double_stop_is_idempotent(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        await handler.start()
        await handler.stop()
        await handler.stop()
        assert handler.is_running is False


# ---------------------------------------------------------------------------
# Event processing tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerEventProcessing:
    @pytest.mark.anyio
    async def test_workflow_started_produces_delta(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler, monitor,
            [_FakeRawEvent(kind="workflow_started", workflow_name="research-flow")],
        )

        assert len(results) == 1
        item = results[0]
        assert item["event_type"] == "workflow.updated"
        assert item["session_id"] == "sess-1"
        assert item["workflow"]["name"] == "research-flow"
        assert item["workflow"]["status"] == "running"

    @pytest.mark.anyio
    async def test_workflow_started_pre_populates_planned_phases_from_agent_core_event(self) -> None:
        """agent-core PhasePlan dataclass must convert to planned phases on the frontend delta."""
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import PhasePlan

        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler,
            monitor,
            [
                _FakeAgentCoreRawEvent(
                    kind="workflow_started",
                    workflow_name="werewolf-game",
                    phases=[
                        PhasePlan(title="发牌", description="分配身份"),
                        PhasePlan(title="游戏进行"),
                    ],
                ),
            ],
        )

        assert len(results) == 1
        phases = results[0]["workflow"]["phases"]
        assert len(phases) == 2
        assert phases[0]["name"] == "发牌"
        assert phases[0]["description"] == "分配身份"
        assert phases[0]["status"] == "planned"
        assert phases[1]["name"] == "游戏进行"
        assert phases[1]["status"] == "planned"

    @pytest.mark.anyio
    async def test_phase_event_ignored_by_handler(self) -> None:
        """PHASE events are ignored; only workflow_started produces a delta."""
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler, monitor,
            [
                _FakeRawEvent(kind="workflow_started", workflow_name="research-flow"),
                _FakeRawEvent(kind="phase", phase="planning"),
            ],
        )

        assert len(results) == 1
        assert results[0]["workflow"]["name"] == "research-flow"

    @pytest.mark.anyio
    async def test_agent_started_produces_phase_delta(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler, monitor,
            [
                _FakeRawEvent(kind="workflow_started", workflow_name="research-flow"),
                _FakeRawEvent(kind="agent_started", phase="planning", label="agent-a"),
            ],
        )

        assert len(results) == 2
        agent_item = results[1]
        assert agent_item["event_type"] == "workflow.updated"
        assert "phases" in agent_item["workflow"]
        assert len(agent_item["workflow"]["phases"]) == 1
        assert agent_item["workflow"]["phases"][0]["name"] == "planning"

    @pytest.mark.anyio
    async def test_log_kind_produces_delta_with_logs(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler, monitor,
            [
                _FakeRawEvent(kind="workflow_started", workflow_name="log-test"),
                _FakeRawEvent(kind="log", text="some log message"),
            ],
        )

        # workflow_started and log both produce deltas; log delta has logs at top level
        assert len(results) == 2
        assert results[0]["workflow"]["name"] == "log-test"
        assert results[1]["workflow"]["logs"] == ["some log message"]

    @pytest.mark.anyio
    async def test_multiple_workflows_sequential(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        results = await _run_handler_with_events(
            handler, monitor,
            [
                _FakeRawEvent(kind="workflow_started", workflow_name="flow-a", run_id="wf_flowa001"),
                _FakeRawEvent(kind="workflow_completed", text="done a", run_id="wf_flowa001"),
                _FakeRawEvent(kind="workflow_started", workflow_name="flow-b", run_id="wf_flowb001"),
            ],
        )

        assert len(results) == 3
        names_in_deltas = [r["workflow"].get("name") for r in results if r["workflow"].get("name")]
        assert "flow-a" in names_in_deltas
        assert "flow-b" in names_in_deltas


# ---------------------------------------------------------------------------
# get_workflow_snapshot tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerGetSnapshot:
    @staticmethod
    def test_get_workflow_snapshot_empty() -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        assert handler.get_workflow_snapshot() == []

    @pytest.mark.anyio
    async def test_get_workflow_snapshot_after_started(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        await _run_handler_with_events(
            handler, monitor,
            [_FakeRawEvent(kind="workflow_started", workflow_name="research-flow")],
        )

        snapshot = handler.get_workflow_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["name"] == "research-flow"
        assert snapshot[0]["status"] == "running"

    @pytest.mark.anyio
    async def test_get_workflow_snapshot_multiple_runs(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        await _run_handler_with_events(
            handler, monitor,
            [
                _FakeRawEvent(kind="workflow_started", workflow_name="flow-a", run_id="wf_flowa001"),
                _FakeRawEvent(kind="workflow_completed", text="done a", run_id="wf_flowa001"),
                _FakeRawEvent(kind="workflow_started", workflow_name="flow-b", run_id="wf_flowb001"),
            ],
        )

        snapshot = handler.get_workflow_snapshot()
        assert len(snapshot) == 2
        names = {s["name"] for s in snapshot}
        assert "flow-a" in names
        assert "flow-b" in names


# ---------------------------------------------------------------------------
# events() async iterator tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerEventsIterator:
    @pytest.mark.anyio
    async def test_events_yields_workflow_updated(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        await handler.start()
        monitor.put_event(_FakeRawEvent(kind="workflow_started", workflow_name="iter-flow"))
        await monitor.drain()
        await handler.stop()

        items: list[dict[str, Any]] = []
        async for item in handler.events():
            items.append(item)

        assert len(items) == 1
        assert items[0]["event_type"] == "workflow.updated"
        assert items[0]["workflow"]["name"] == "iter-flow"

    @pytest.mark.anyio
    async def test_events_terminates_after_stop(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        await handler.start()
        await handler.stop()

        yielded = False
        async for _ in handler.events():
            yielded = True
        assert not yielded


# ---------------------------------------------------------------------------
# Temp-key rekey tests
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerRunIdRegistry:
    @pytest.mark.anyio
    async def test_run_id_used_as_registry_key(self) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")
        run_id = "wf_explicitrun01"

        await _run_handler_with_events(
            handler, monitor,
            [_FakeRawEvent(kind="workflow_started", workflow_name="research-flow", run_id=run_id)],
        )

        runs = handler.get_run_states()
        assert len(runs) == 1
        assert run_id in runs
        assert runs[run_id].id == run_id


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------

class TestWorkflowMonitorHandlerPersist:
    """_persist() must land runs + session_budget in ONE read-modify-write.

    Regression: two separate persists each cache_bust-read the disk before
    the other's async-queued write is flushed; the second full-file replace
    reverts the first's workflow_runs (lost update freezing the checkpoint
    at a stale pre-terminal state).
    """

    def test_persist_writes_runs_and_budget_in_single_write(self) -> None:
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-persist")
        handler._runs = {
            "wf_run1": WorkflowRunState(
                id="wf_run1", name="flow", status="stopped",
                agent_count=5, completed_agent_count=5,
            )
        }
        budget = {"total": 500000, "spent": 400000, "remaining": 100000, "scope": "session", "exhausted": False}
        handler._session_budget = budget

        store: dict[str, Any] = {"session_id": "sess-persist", "title": "t"}
        written: list[tuple[str, dict]] = []
        original_read = sm._read_metadata
        original_enqueue = sm._enqueue_write
        sm._read_metadata = lambda session_id, cache_bust=True: dict(store)
        sm._enqueue_write = lambda session_id, metadata: written.append((session_id, dict(metadata)))
        try:
            handler._persist()
        finally:
            sm._read_metadata = original_read
            sm._enqueue_write = original_enqueue

        assert len(written) == 1, "runs + budget must share one enqueue (single RMW)"
        session_id, payload = written[0]
        assert session_id == "sess-persist"
        assert payload["session_budget"] == budget
        persisted_runs = payload.get("workflow_runs") or {}
        assert persisted_runs["wf_run1"]["status"] == "stopped"
        assert persisted_runs["wf_run1"]["completed_agent_count"] == 5

    def test_persist_without_budget_only_writes_runs(self) -> None:
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-persist2")
        handler._runs = {"wf_run2": WorkflowRunState(id="wf_run2", name="flow", status="running")}

        written: list[tuple[str, dict]] = []
        original_read = sm._read_metadata
        original_enqueue = sm._enqueue_write
        sm._read_metadata = lambda session_id, cache_bust=True: {"session_id": "sess-persist2"}
        sm._enqueue_write = lambda session_id, metadata: written.append((session_id, dict(metadata)))
        try:
            handler._persist()
        finally:
            sm._read_metadata = original_read
            sm._enqueue_write = original_enqueue

        assert len(written) == 1
        assert "session_budget" not in written[0][1]
        assert "wf_run2" in (written[0][1].get("workflow_runs") or {})


# ---------------------------------------------------------------------------
# New-field passthrough (agent_id / node_type / correlation_id / answer)
# ---------------------------------------------------------------------------

class TestExtractProgressPassthrough:
    """_extract_progress passes the new fields through the dict payload path.

    Agent-core EventMessage / WorkflowProgressTeamEvent shapes vary by package
    version; those paths are not pinned here.
    """

    @staticmethod
    def test_dict_payload_passes_new_fields() -> None:
        """dict path: WorkflowProgress(**payload) covers the new fields."""
        handler = WorkflowMonitorHandler(monitor=_FakeTeamMonitor(), session_id="s")
        payload = {
            "kind": "agent_started",
            "phase": "review",
            "label": "host",
            "agent_id": "main/call:1",
            "node_type": "human_session",
            "correlation_id": "review:host:0",
            "answer": "yes, approved",
        }
        progress = handler._extract_progress(SimpleNamespace(payload=payload))
        assert progress is not None
        assert progress.agent_id == "main/call:1"
        assert progress.node_type == "human_session"
        assert progress.correlation_id == "review:host:0"
        assert progress.answer == "yes, approved"

    @staticmethod
    def test_object_fallback_copies_new_fields() -> None:
        """object fallback path copies tokens/budget/nested_* onto WorkflowProgress.

        A raw payload that is neither a dict nor a pydantic model (no
        ``model_dump``) must still surface the SDD-0010 fields via
        ``getattr`` so the fallback branch does not silently drop them.
        """
        h = WorkflowMonitorHandler.__new__(WorkflowMonitorHandler)
        h._session_id = "s"

        class _Payload:
            kind = "agent_completed"
            run_id = "wf_1"; workflow_name = "w"; description = None; phase = "review"
            label = "analyst"; prompt = None; model = None; outcome = "ok"; text = None
            phases = None; correlation_id = None; node_type = "agent"; agent_id = "k1"; answer = None
            tokens = 12700
            budget = {"total": 5, "spent": 5, "remaining": 0, "scope": "leader", "exhausted": True}
            phase_type = "child"
            nested_phase = "▸ intro #0"
            parent_phase = "review"
            script_path = "/abs/path/foo.py"

        class _Ev:
            def get_payload(self):  # noqa: ANN202
                return _Payload()

        p = h._extract_progress(_Ev())
        assert p.tokens == 12700
        assert p.budget["exhausted"] is True
        assert p.phase_type == "child"
        assert p.nested_phase == "▸ intro #0"
        assert p.parent_phase == "review"
        assert p.script_path == "/abs/path/foo.py"


# ---------------------------------------------------------------------------
# workflow_started script_path passthrough (冷启动续跑情境注入)
# ---------------------------------------------------------------------------

class TestWorkflowStartedScriptPath:
    @pytest.mark.anyio
    async def test_workflow_started_carries_script_path_to_run_state(self) -> None:
        """workflow_started 携带 script_path → handler 的 run state 记录该路径。

        The engine carries the script's absolute path on workflow_started so a
        cold-start resume can inject it as context to the leader; the handler
        must surface it on the WorkflowRunState, not drop it.
        """
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(monitor=monitor, session_id="sess-1")

        await _run_handler_with_events(
            handler, monitor,
            [_FakeRawEvent(kind="workflow_started", workflow_name="research-flow",
                           script_path="/abs/path/foo.py")],
        )

        runs = handler.get_run_states()
        assert len(runs) == 1
        run = next(iter(runs.values()))
        assert run.script_path == "/abs/path/foo.py"


# ---------------------------------------------------------------------------
# finalize_pending_runs: disposition-aware finalize (not one-size stopped)
# ---------------------------------------------------------------------------

def _running_run(run_id: str = "wf_run_running") -> WorkflowRunState:
    """A running run with one running phase carrying one running agent."""
    from jiuwenswarm.agents.harness.team.handlers.workflow_state import (
        WorkflowRunState, WorkflowPhaseState, WorkflowAgentState,
    )
    return WorkflowRunState(
        id=run_id,
        name="flow",
        status="running",
        started_at="2026-09-07T10:00:00+08:00",
        phases=[
            WorkflowPhaseState(
                id="p1",
                name="Phase 1",
                status="running",
                agents=[WorkflowAgentState(id="a1", name="agent-a", status="running")],
            )
        ],
    )


class TestFinalizePendingRunsDisposition:
    """finalize by disposition, not one-size stopped.

    A session teardown must not stamp a paused (resumable) run to the terminal
    ``stopped`` — pause reclaim parks it as ``paused`` so the journal cache
    prefix survives cold start; only an explicit user stop stamps ``stopped``.
    """

    def test_paused_run_stays_paused_under_stop_disposition(self, monkeypatch) -> None:
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_paused": WorkflowRunState(id="wf_paused", name="flow", status="paused")},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="stop")
        assert handler.get_run_states()["wf_paused"].status == "paused"

    def test_paused_run_stays_paused_under_pause_disposition(self, monkeypatch) -> None:
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_paused": WorkflowRunState(id="wf_paused", name="flow", status="paused")},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="pause")
        assert handler.get_run_states()["wf_paused"].status == "paused"

    def test_running_run_stopped_under_stop_disposition(self, monkeypatch) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_run": _running_run()},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="stop")
        run = handler.get_run_states()["wf_run"]
        assert run.status == "stopped"
        assert run.is_terminal is True
        assert run.completed_at is not None
        assert run.phases[0].status == "stopped"
        assert run.phases[0].agents[0].status == "stopped"
        assert run.phases[0].agents[0].completed_at is not None
        assert run.completed_agent_count == 1

    def test_running_run_paused_under_pause_disposition(self, monkeypatch) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_run": _running_run()},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="pause")
        run = handler.get_run_states()["wf_run"]
        assert run.status == "paused"
        assert run.is_terminal is False
        assert run.completed_at is None  # paused parks, never stamps terminal fields
        assert run.duration_ms is None
        assert run.phases[0].status == "paused"
        assert run.phases[0].agents[0].status == "paused"
        assert run.phases[0].agents[0].completed_at is None

    @staticmethod
    def _make_terminal_run(run_id: str = "wf_completed") -> WorkflowRunState:
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        return WorkflowRunState(
            id=run_id, name="flow", status="completed",
            completed_at="2026-09-07T10:05:00+08:00",
        )

    def test_terminal_run_untouched_under_stop_disposition(self, monkeypatch) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_completed": self._make_terminal_run()},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="stop")
        run = handler.get_run_states()["wf_completed"]
        assert run.status == "completed"
        assert run.completed_at == "2026-09-07T10:05:00+08:00"

    def test_terminal_run_untouched_under_pause_disposition(self, monkeypatch) -> None:
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={"wf_completed": self._make_terminal_run()},
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="pause")
        run = handler.get_run_states()["wf_completed"]
        assert run.status == "completed"

    def test_mixed_runs_finalize_by_disposition(self, monkeypatch) -> None:
        """One running + one already-paused run under stop: only the running one is stopped."""
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        monitor = _FakeTeamMonitor()
        handler = WorkflowMonitorHandler(
            monitor=monitor, session_id="sess-1",
            initial_runs={
                "wf_run": _running_run("wf_run"),
                "wf_paused": WorkflowRunState(id="wf_paused", name="flow", status="paused"),
            },
        )
        monkeypatch.setattr(handler, "_persist", lambda: None)
        handler.finalize_pending_runs(disposition="stop")
        runs = handler.get_run_states()
        assert runs["wf_run"].status == "stopped"
        assert runs["wf_paused"].status == "paused"


# ---------------------------------------------------------------------------
# stop_run: tree-view stop on a paused run must reach the frontend
# ---------------------------------------------------------------------------


class TestStopRun:
    @staticmethod
    @pytest.mark.asyncio
    async def test_stop_run_emits_terminal_delta_and_persists(monkeypatch) -> None:
        """A paused run has no engine task left to emit WORKFLOW_STOPPED, so the
        handler must synthesize it: terminal delta on the event queue (tree
        refresh) + persisted snapshot. Same path as an engine stop.
        """
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState

        run = WorkflowRunState(status="paused")
        run.id = "wf_1"
        handler = WorkflowMonitorHandler(
            monitor=_FakeTeamMonitor(), session_id="sess-1", initial_runs={"wf_1": run},
        )
        persisted: list[int] = []
        monkeypatch.setattr(handler, "_persist", lambda: persisted.append(1))

        assert await handler.stop_run("wf_1") is True

        assert run.status == "stopped"
        assert persisted == [1]
        event = handler._event_queue.get_nowait()
        assert event["event_type"] == "workflow.updated"
        assert event["workflow"]["id"] == "wf_1"
        assert event["workflow"]["status"] == "stopped"
        # unknown / already terminal → no-op
        assert await handler.stop_run("wf_1") is False
        assert await handler.stop_run("nope") is False
        # active run: the engine announces its own stop while unwinding → no-op
        live = WorkflowRunState(status="running")
        live.id = "wf_live"
        handler._runs["wf_live"] = live
        assert await handler.stop_run("wf_live") is False
        assert live.status == "running"
        assert handler._event_queue.empty()
