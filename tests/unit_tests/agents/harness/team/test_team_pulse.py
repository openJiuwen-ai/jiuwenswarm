# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TeamPulse detection, deterministic recovery, and monitor attachment."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType

from jiuwenswarm.agents.harness.team.pulse.invariants import detect_conflicts
from jiuwenswarm.agents.harness.team.pulse.models import (
    DEPENDENCY_VIOLATION,
    DUPLICATE_ACTIVE,
    INVALID_TRANSITION,
    OWNERSHIP_UNRESOLVED,
    PERMISSION_VIOLATION,
    STALE_OWNERSHIP,
    UNAVAILABLE_OWNER,
    Conflict,
    CoordinationSnapshot,
    Decision,
    DecisionKind,
    MemberFact,
    TaskFact,
)
from jiuwenswarm.agents.harness.team.pulse.recovery import DeterministicRecovery
from jiuwenswarm.agents.harness.team.pulse.service import (
    MonitorNativeSource,
    TeamPulseService,
    conflict_fingerprint,
)
from jiuwenswarm.agents.harness.team.pulse.snapshot import fold_monitor_event
from jiuwenswarm.agents.harness.team.team_manager import TeamManager


class FakeSource:
    """Native rows plus a reset port that records calls and does not change rows."""

    def __init__(
        self,
        tasks: list[TaskFact],
        members: list[MemberFact] | None = None,
        leader: str = "leader",
    ) -> None:
        self.tasks = list(tasks)
        self.members = list(members if members is not None else _roster())
        self.leader = leader
        self.reset_ids: list[str] = []
        self.loads = 0

    async def load(self) -> tuple[str | None, list[TaskFact], list[MemberFact]]:
        self.loads += 1
        return self.leader, list(self.tasks), list(self.members)

    async def reset_task(self, task_id: str) -> bool:
        self.reset_ids.append(task_id)
        return True


class EscalateAll:
    """Stand-in for a future solver: it sees the same conflicts and writes nothing."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def propose(self, conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
        self.seen.append(conflict.conflict_type)
        return Decision(kind=DecisionKind.ESCALATE, reason="substitute strategy")


def _task(
    task_id: str = "task-1",
    *,
    status: str = "in_progress",
    assignee: str | None = "alpha",
    updated_at: int | None = 1,
    blocked_by: tuple[str, ...] = (),
    summary: str | None = None,
    note: str | None = None,
) -> TaskFact:
    return TaskFact(
        task_id=task_id,
        status=status,
        assignee=assignee,
        updated_at=updated_at,
        blocked_by=blocked_by,
        summary=summary,
        note=note,
    )


def _member(name: str, status: str = "ready", role: str = "teammate") -> MemberFact:
    return MemberFact(member_name=name, status=status, role=role)


def _roster(*extra: MemberFact) -> list[MemberFact]:
    return [_member("leader", role="leader"), _member("alpha"), *extra]


def _snapshot(
    tasks: list[TaskFact],
    members: list[MemberFact] | None = None,
    *,
    previous_status: dict[str, str] | None = None,
    plan_requests: set[str] | None = None,
) -> CoordinationSnapshot:
    roster = members if members is not None else _roster()
    snapshot = CoordinationSnapshot(
        team_name="team",
        leader_member_name="leader",
        tasks={task.task_id: task for task in tasks},
        members={member.member_name: member for member in roster},
        last_event_type="task_updated",
        last_event_timestamp=10,
    )
    if previous_status:
        snapshot.previous_status.update(previous_status)
    if plan_requests:
        snapshot.plan_requests.update(plan_requests)
    return snapshot


def _service(source: FakeSource, strategy=None) -> TeamPulseService:
    return TeamPulseService(
        session_id="sess",
        team_name="team",
        source=source,
        strategy=strategy,
    )


def _event(
    event_type: MonitorEventType,
    *,
    task_id: str | None = None,
    member_name: str | None = None,
    timestamp: int = 1,
) -> MonitorEvent:
    return MonitorEvent(
        event_type=event_type,
        team_name="team",
        task_id=task_id,
        member_name=member_name,
        timestamp=timestamp,
    )


def _assert_evidence(conflict: Conflict) -> None:
    assert conflict.invariant
    assert conflict.evidence.event_type
    assert conflict.evidence.timestamp is not None
    assert isinstance(conflict.evidence.previous, dict)
    assert conflict.evidence.current
    assert conflict.impact.value in {"blocks_execution", "needs_leader"}


def test_stale_ownership_adopts_native_assignee_without_a_write() -> None:
    snapshot = _snapshot([_task(assignee="alpha")])
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="alpha", timestamp=1))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="beta", timestamp=2))
    conflicts = detect_conflicts(snapshot)

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.conflict_type == STALE_OWNERSHIP
    assert conflict.task_ids == ("task-1",)
    assert conflict.member_names == ("alpha", "beta")
    assert conflict.evidence.current["assignee"] == "alpha"
    assert conflict.evidence.current["claimants"] == ["alpha", "beta"]
    _assert_evidence(conflict)
    decision = DeterministicRecovery().propose(conflict, snapshot)
    assert decision.kind is DecisionKind.RECONCILE
    assert decision.reset_task_ids == ()


def test_claim_after_release_is_not_an_ownership_conflict() -> None:
    snapshot = _snapshot([_task(assignee="beta")], _roster(_member("beta")))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="alpha", timestamp=1))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_RELEASED, task_id="task-1", timestamp=2))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="beta", timestamp=3))

    assert detect_conflicts(snapshot) == []


def test_unresolved_ownership_escalates() -> None:
    snapshot = _snapshot([_task(assignee="gamma")], _roster(_member("beta"), _member("gamma")))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="alpha", timestamp=1))
    fold_monitor_event(snapshot, _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="beta", timestamp=2))
    conflicts = detect_conflicts(snapshot)

    assert [item.conflict_type for item in conflicts] == [OWNERSHIP_UNRESOLVED]
    assert conflicts[0].evidence.current["assignee"] == "gamma"
    _assert_evidence(conflicts[0])
    assert DeterministicRecovery().propose(conflicts[0], snapshot).kind is DecisionKind.ESCALATE


def test_dependency_violation_escalates_and_pending_blocked_work_does_not() -> None:
    executing = _snapshot([_task(status="in_progress", blocked_by=("dep-1",))])
    waiting = _snapshot([_task(status="pending", blocked_by=("dep-1",))])
    conflicts = detect_conflicts(executing)

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == DEPENDENCY_VIOLATION
    assert conflicts[0].evidence.current["blocked_by"] == ["dep-1"]
    assert conflicts[0].task_ids == ("task-1",)
    _assert_evidence(conflicts[0])
    assert DeterministicRecovery().propose(conflicts[0], executing).kind is DecisionKind.ESCALATE
    assert detect_conflicts(waiting) == []


def test_invalid_transition_escalates_and_a_legal_start_does_not() -> None:
    illegal = _snapshot([_task(status="completed")], previous_status={"task-1": "pending"})
    legal = _snapshot([_task(status="in_progress")], previous_status={"task-1": "pending"})
    conflicts = detect_conflicts(illegal)

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == INVALID_TRANSITION
    assert conflicts[0].evidence.previous == {"status": "pending"}
    assert conflicts[0].evidence.current["status"] == "completed"
    _assert_evidence(conflicts[0])
    assert DeterministicRecovery().propose(conflicts[0], illegal).kind is DecisionKind.ESCALATE
    assert detect_conflicts(legal) == []


def test_duplicate_active_keeps_the_only_task_with_evidence() -> None:
    snapshot = _snapshot(
        [
            _task("task-a", status="in_review", updated_at=1),
            _task("task-b", status="in_progress", updated_at=2),
        ]
    )
    conflicts = detect_conflicts(snapshot)

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == DUPLICATE_ACTIVE
    assert conflicts[0].member_names == ("alpha",)
    assert conflicts[0].evidence.current["tasks"][0]["task_id"] == "task-a"
    _assert_evidence(conflicts[0])
    decision = DeterministicRecovery().propose(conflicts[0], snapshot)
    assert decision.kind is DecisionKind.APPLY
    assert decision.reset_task_ids == ("task-b",)


def test_duplicate_active_without_evidence_keeps_the_earliest_task() -> None:
    snapshot = _snapshot(
        [
            _task("task-b", updated_at=20),
            _task("task-a", updated_at=10),
            _task("task-c", updated_at=None),
        ]
    )
    decision = DeterministicRecovery().propose(detect_conflicts(snapshot)[0], snapshot)
    assert decision.reset_task_ids == ("task-b", "task-c")


def test_duplicate_active_with_two_evidence_records_escalates() -> None:
    snapshot = _snapshot(
        [
            _task("task-a", summary="shipped"),
            _task("task-b", note="still writing"),
        ]
    )
    conflict = detect_conflicts(snapshot)[0]
    _assert_evidence(conflict)
    decision = DeterministicRecovery().propose(conflict, snapshot)
    assert decision.kind is DecisionKind.ESCALATE
    assert decision.reset_task_ids == ()


def test_unavailable_owner_releases_only_unstarted_planning() -> None:
    members = _roster(_member("alpha", status="shut_down"))
    safe = _snapshot([_task(status="planning")], members)
    requested = _snapshot([_task(status="planning")], members, plan_requests={"task-1"})
    running = _snapshot([_task(status="in_progress", note="partial")], members)
    pending = _snapshot([_task(status="pending", assignee="alpha")], members)

    safe_conflict = detect_conflicts(safe)[0]
    assert safe_conflict.conflict_type == UNAVAILABLE_OWNER
    assert safe_conflict.evidence.current["member_status"] == "shut_down"
    assert safe_conflict.evidence.current["plan_requested"] is False
    _assert_evidence(safe_conflict)
    assert DeterministicRecovery().propose(safe_conflict, safe).reset_task_ids == ("task-1",)
    assert DeterministicRecovery().propose(detect_conflicts(requested)[0], requested).kind is DecisionKind.ESCALATE
    assert DeterministicRecovery().propose(detect_conflicts(running)[0], running).kind is DecisionKind.ESCALATE
    assert DeterministicRecovery().propose(detect_conflicts(pending)[0], pending).kind is DecisionKind.ESCALATE


def test_permission_violation_resets_only_a_blank_resettable_task() -> None:
    leader_owned = _snapshot([_task(status="planning", assignee="leader")])
    noted = _snapshot([_task(status="in_progress", assignee="leader", summary="kept")])
    missing = _snapshot([_task(status="in_progress", assignee="ghost")])
    pending = _snapshot([_task(status="pending", assignee="leader")])

    leader_conflict = detect_conflicts(leader_owned)[0]
    assert leader_conflict.conflict_type == PERMISSION_VIOLATION
    assert leader_conflict.evidence.current["leader_member_name"] == "leader"
    assert leader_conflict.member_names == ("leader",)
    _assert_evidence(leader_conflict)
    assert DeterministicRecovery().propose(leader_conflict, leader_owned).reset_task_ids == ("task-1",)
    assert DeterministicRecovery().propose(detect_conflicts(missing)[0], missing).kind is DecisionKind.APPLY
    assert DeterministicRecovery().propose(detect_conflicts(noted)[0], noted).kind is DecisionKind.ESCALATE
    assert DeterministicRecovery().propose(detect_conflicts(pending)[0], pending).kind is DecisionKind.ESCALATE


async def test_service_resets_duplicate_work_once() -> None:
    source = FakeSource(
        [
            _task("task-a", status="in_review", updated_at=1),
            _task("task-b", status="in_progress", updated_at=2),
        ]
    )
    service = _service(source)

    first = await service.scan()
    second = await service.scan()

    assert source.reset_ids == ["task-b"]
    assert [entry.decision.kind for entry in first] == [DecisionKind.APPLY]
    assert first[0].applied is True
    assert second == []
    assert len(service.journal) == 1


async def test_service_resets_abandoned_planning_once() -> None:
    source = FakeSource(
        [_task(status="planning")],
        _roster(_member("alpha", status="error")),
    )
    service = _service(source)

    await service.scan()
    await service.scan()

    assert source.reset_ids == ["task-1"]
    assert service.journal[0].conflict.conflict_type == UNAVAILABLE_OWNER
    assert service.journal[0].decision.kind is DecisionKind.APPLY


async def test_service_escalates_ambiguous_and_illegal_states_without_reset() -> None:
    source = FakeSource(
        [
            _task("task-a", summary="one"),
            _task("task-b", note="two"),
            _task("task-dep", status="completed", assignee="alpha", blocked_by=("dep-1",)),
        ]
    )
    service = _service(source)
    await service.scan()
    source.tasks = [_task("task-jump", status="pending")]
    await service.scan()
    source.tasks = [_task("task-jump", status="completed")]
    await service.scan()

    assert source.reset_ids == []
    kinds = [entry.conflict.conflict_type for entry in service.journal]
    assert DUPLICATE_ACTIVE in kinds
    assert DEPENDENCY_VIOLATION in kinds
    assert INVALID_TRANSITION in kinds
    assert all(entry.decision.kind is DecisionKind.ESCALATE for entry in service.journal)


async def test_service_reconciles_a_stale_claim_and_escalates_an_unknown_assignee() -> None:
    stale = FakeSource([_task(assignee="alpha")], _roster(_member("beta")))
    service = _service(stale)
    await service.handle_event(
        _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="alpha", timestamp=1)
    )
    await service.handle_event(
        _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="beta", timestamp=2)
    )
    await service.scan()

    assert stale.reset_ids == []
    assert [entry.conflict.conflict_type for entry in service.journal] == [STALE_OWNERSHIP]
    assert service.journal[0].decision.kind is DecisionKind.RECONCILE
    assert service.journal[0].applied is True

    unresolved = FakeSource([_task(assignee="gamma")], _roster(_member("beta"), _member("gamma")))
    other = _service(unresolved)
    await other.handle_event(
        _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="alpha", timestamp=3)
    )
    await other.handle_event(
        _event(MonitorEventType.TASK_CLAIMED, task_id="task-1", member_name="beta", timestamp=4)
    )

    assert unresolved.reset_ids == []
    assert other.journal[-1].conflict.conflict_type == OWNERSHIP_UNRESOLVED
    assert other.journal[-1].decision.kind is DecisionKind.ESCALATE


async def test_service_escalates_in_progress_work_held_by_a_departed_member() -> None:
    source = FakeSource(
        [_task(status="in_progress", note="partial")],
        _roster(_member("alpha", status="shutdown_requested")),
    )
    service = _service(source)

    entries = await service.scan()

    assert source.reset_ids == []
    assert entries[0].conflict.conflict_type == UNAVAILABLE_OWNER
    assert entries[0].decision.kind is DecisionKind.ESCALATE
    assert entries[0].applied is False


async def test_plan_request_blocks_automatic_release() -> None:
    source = FakeSource(
        [_task(status="planning")],
        _roster(_member("alpha", status="shut_down")),
    )
    service = _service(source)
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    pulse_logger = logging.getLogger("jiuwenswarm.agents.harness.team.pulse.service")
    pulse_logger.addHandler(handler)
    try:
        await service.handle_event(
            _event(MonitorEventType.TASK_PLAN_REQUEST, task_id="task-1", member_name="alpha", timestamp=8)
        )
    finally:
        pulse_logger.removeHandler(handler)

    assert source.reset_ids == []
    assert service.journal[0].decision.kind is DecisionKind.ESCALATE
    assert any(message.startswith("teampulse.conflict") for message in messages)


async def test_message_events_do_not_scan() -> None:
    source = FakeSource([_task()])
    service = _service(source)

    entries = await service.handle_event(
        _event(MonitorEventType.MESSAGE, member_name="alpha", timestamp=1)
    )

    assert entries == []
    assert source.loads == 0
    assert service.journal == ()


async def test_substitute_strategy_receives_conflicts_and_does_not_reset() -> None:
    source = FakeSource(
        [
            _task("task-a", updated_at=1),
            _task("task-b", updated_at=2),
        ]
    )
    strategy = EscalateAll()
    service = _service(source, strategy=strategy)

    entries = await service.scan()

    assert strategy.seen == [DUPLICATE_ACTIVE]
    assert source.reset_ids == []
    assert entries[0].decision.reason == "substitute strategy"
    assert not hasattr(
        __import__("jiuwenswarm.agents.harness.team.pulse.recovery", fromlist=["recovery"]),
        "ConstraintSolver",
    )


async def test_rejected_reset_is_escalated_and_not_retried() -> None:
    class RejectingSource(FakeSource):
        async def reset_task(self, task_id: str) -> bool:
            self.reset_ids.append(task_id)
            return False

    source = RejectingSource([_task(status="planning")], _roster(_member("alpha", status="shut_down")))
    service = _service(source)

    first = await service.scan()
    second = await service.scan()

    assert source.reset_ids == ["task-1"]
    assert first[0].decision.kind is DecisionKind.ESCALATE
    assert first[0].applied is False
    assert second == []


def test_conflict_fingerprint_is_stable() -> None:
    snapshot = _snapshot([_task(status="planning", assignee="leader")])
    conflict = detect_conflicts(snapshot)[0]
    assert conflict_fingerprint(conflict) == conflict_fingerprint(conflict)


async def test_monitor_source_reads_task_detail_and_resets() -> None:
    detail = SimpleNamespace(
        status="in_progress",
        assignee="alpha",
        updated_at=5,
        blocked_by=["dep-1"],
        summary="shipped",
        note="path",
    )

    class Manager:
        def __init__(self) -> None:
            self.reset_ids: list[str] = []

        async def get_task_detail(self, task_id: str):
            assert task_id == "task-1"
            return detail

        async def reset(self, task_id: str):
            self.reset_ids.append(task_id)
            return SimpleNamespace(ok=True)

    manager = Manager()
    monitor = SimpleNamespace(
        team_name="team",
        session_id="sess-native",
        team_agent=SimpleNamespace(task_manager=manager),
        get_team_info=_async_return(SimpleNamespace(leader_member_name="leader")),
        get_tasks=_async_return([SimpleNamespace(task_id="task-1", status="pending", assignee=None, updated_at=1)]),
        get_members=_async_return([SimpleNamespace(member_name="alpha", status="ready", role="teammate")]),
    )
    source = MonitorNativeSource(monitor)

    leader, tasks, members = await source.load()

    assert leader == "leader"
    assert tasks[0].blocked_by == ("dep-1",)
    assert tasks[0].summary == "shipped"
    assert tasks[0].status == "in_progress"
    assert members[0].member_name == "alpha"
    assert await source.reset_task("task-1") is True
    assert manager.reset_ids == ["task-1"]


async def test_cleanup_stops_registered_pulse() -> None:
    manager = TeamManager()
    stopped = False

    class Pulse:
        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    manager.register_team_pulse("sess", Pulse())
    await manager._cleanup_runtime_locals("sess")

    assert stopped is True
    assert manager.get_team_pulse("sess") is None


async def test_active_runtime_requests_team_pulse(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    agent = object()

    class Handler:
        is_running = True
        bound_team_agent = agent

    class Manager:
        def get_monitor(self, session_id: str) -> Handler:
            return Handler()

    monitor = SimpleNamespace(team_agent=agent)
    seen: dict[str, object] = {}

    async def fake_get(*_args, **_kwargs):
        return monitor

    async def fake_ensure(tm, session_id, team_name, current):
        seen["session_id"] = session_id
        seen["team_name"] = team_name
        seen["monitor"] = current
        seen["manager"] = tm

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _channel: Manager())
    monkeypatch.setattr(team_helpers, "_get_active_team_monitor", fake_get)
    monkeypatch.setattr(team_helpers, "_ensure_team_pulse", fake_ensure)

    await team_helpers.ensure_monitor_handlers_for_active_runtime("channel", "sess", "team-a")

    assert seen["session_id"] == "sess"
    assert seen["team_name"] == "team-a"
    assert seen["monitor"] is monitor


async def test_ensure_team_pulse_starts_once_for_the_current_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    agent = object()
    pulse_monitor = _BlockingMonitor(agent)
    fetches = 0

    async def fake_get(*_args, **_kwargs):
        nonlocal fetches
        fetches += 1
        return pulse_monitor

    class Manager:
        def __init__(self) -> None:
            self.pulse = None

        def get_team_pulse(self, session_id: str):
            return self.pulse

        def pop_team_pulse(self, session_id: str):
            pulse = self.pulse
            self.pulse = None
            return pulse

        def register_team_pulse(self, session_id: str, service) -> None:
            self.pulse = service

    manager = Manager()
    monkeypatch.setattr(team_helpers, "_get_active_team_monitor", fake_get)
    current = SimpleNamespace(team_agent=agent)

    await team_helpers._ensure_team_pulse(manager, "sess", "team-a", current)
    await team_helpers._ensure_team_pulse(manager, "sess", "team-a", current)

    assert fetches == 1
    assert manager.pulse is not None
    assert manager.pulse.is_running is True
    await manager.pulse.stop()


async def test_ensure_team_pulse_start_failure_does_not_register(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    created: list[object] = []

    class Boom:
        def __init__(self, **_kwargs) -> None:
            self.stopped = False
            created.append(self)

        async def start(self, _monitor) -> None:
            raise RuntimeError("monitor down")

        async def stop(self) -> None:
            self.stopped = True

    class Manager:
        def __init__(self) -> None:
            self.pulse = None

        def get_team_pulse(self, session_id: str):
            return self.pulse

        def pop_team_pulse(self, session_id: str):
            pulse = self.pulse
            self.pulse = None
            return pulse

        def register_team_pulse(self, session_id: str, service) -> None:
            self.pulse = service

    async def fake_get(*_args, **_kwargs):
        return _BlockingMonitor(object())

    monkeypatch.setattr(team_helpers, "TeamPulseService", Boom)
    monkeypatch.setattr(team_helpers, "_get_active_team_monitor", fake_get)
    manager = Manager()

    await team_helpers._ensure_team_pulse(
        manager,
        "sess",
        "team-a",
        SimpleNamespace(team_agent=object()),
    )

    assert manager.pulse is None
    assert created[0].stopped is True


class _BlockingMonitor:
    def __init__(self, agent: object) -> None:
        self.team_agent = agent
        self._workflow_event_queue: asyncio.Queue = asyncio.Queue()
        self._event_queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._event_queue.put_nowait(None)

    def events(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            item = await self._event_queue.get()
            if item is None:
                break
            yield item


def _async_return(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call

