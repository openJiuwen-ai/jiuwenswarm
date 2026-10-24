# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Event-driven TeamPulse service.

Listens on its own TeamMonitor, refreshes native rows, and applies a
recovery strategy. It does not share the UI monitor queue and it does not
call an LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from openjiuwen.agent_teams.context import get_session_id, reset_session_id, set_session_id

from jiuwenswarm.agents.harness.team.handlers.base_monitor_handler import (
    monitor_team_agent,
)
from jiuwenswarm.agents.harness.team.pulse.invariants import detect_conflicts
from jiuwenswarm.agents.harness.team.pulse.models import (
    Conflict,
    CoordinationSnapshot,
    Decision,
    DecisionKind,
    JournalEntry,
    MemberFact,
    TaskFact,
)
from jiuwenswarm.agents.harness.team.pulse.recovery import DeterministicRecovery, RecoveryStrategy
from jiuwenswarm.agents.harness.team.pulse.snapshot import (
    adopt_recorded_assignee,
    fold_monitor_event,
    install_native_rows,
    is_coordination_event,
)

logger = logging.getLogger(__name__)

_JOURNAL_LIMIT = 256


class NativeCoordinationSource(Protocol):
    """Read native coordination rows and release a task through reset."""

    async def load(self) -> tuple[str | None, list[TaskFact], list[MemberFact]]:
        """Return the leader name, task facts, and member facts."""

    async def reset_task(self, task_id: str) -> bool:
        """Release one task. True when the existing reset API accepted it."""


class MonitorNativeSource:
    """Native reads and reset through a leader TeamMonitor."""

    def __init__(self, monitor: Any) -> None:
        self._monitor = monitor
        self._warned_missing_manager = False

    async def load(self) -> tuple[str | None, list[TaskFact], list[MemberFact]]:
        with _bound_monitor_session(self._monitor):
            info = await self._monitor.get_team_info()
            leader = getattr(info, "leader_member_name", None) if info is not None else None
            task_rows = await self._monitor.get_tasks() or []
            member_rows = await self._monitor.get_members() or []
            manager = _task_manager(self._monitor)
            if manager is None and not self._warned_missing_manager:
                self._warned_missing_manager = True
                logger.warning(
                    "teampulse.conflict team=%s task manager unavailable; "
                    "dependency and execution-evidence fields are empty",
                    getattr(self._monitor, "team_name", ""),
                )
            tasks = [await _task_fact(manager, row) for row in task_rows]
            members = [
                MemberFact(
                    member_name=row.member_name,
                    status=row.status,
                    role=getattr(row, "role", "") or "",
                )
                for row in member_rows
            ]
        return leader, tasks, members

    async def reset_task(self, task_id: str) -> bool:
        manager = _task_manager(self._monitor)
        if manager is None:
            return False
        with _bound_monitor_session(self._monitor):
            result = await manager.reset(task_id)
        return bool(getattr(result, "ok", result))


class TeamPulseService:
    """Watch one team and journal every conflict and recovery decision."""

    def __init__(
        self,
        *,
        session_id: str,
        team_name: str,
        source: NativeCoordinationSource,
        strategy: RecoveryStrategy | None = None,
    ) -> None:
        self._session_id = session_id
        self._team_name = team_name
        self._source = source
        self._strategy = strategy if strategy is not None else DeterministicRecovery()
        self._snapshot = CoordinationSnapshot(team_name=team_name)
        self._journal: list[JournalEntry] = []
        self._applied: set[str] = set()
        self._monitor: Any = None
        self._task: asyncio.Task | None = None
        self._workflow_task: asyncio.Task | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def team_name(self) -> str:
        return self._team_name

    @property
    def snapshot(self) -> CoordinationSnapshot:
        return self._snapshot

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return tuple(self._journal)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def bound_team_agent(self) -> Any:
        """Leader the pulse monitor listens on, or None before start."""
        if self._monitor is None:
            return None
        return monitor_team_agent(self._monitor)

    async def start(self, monitor: Any) -> None:
        """Start consuming ``monitor``. The caller passes a dedicated monitor."""
        if self.is_running:
            return
        self._monitor = monitor
        await monitor.start()
        self._workflow_task = asyncio.create_task(
            self._discard_workflow_events(),
            name=f"teampulse-workflow-{self._session_id}",
        )
        self._task = asyncio.create_task(
            self._consume(),
            name=f"teampulse-{self._session_id}",
        )

    async def stop(self) -> None:
        """Stop the monitor and the consume task. Safe to call twice."""
        task = self._task
        workflow_task = self._workflow_task
        self._task = None
        self._workflow_task = None
        monitor = self._monitor
        if monitor is not None:
            try:
                await monitor.stop()
            except Exception as exc:
                logger.warning(
                    "teampulse.conflict session_id=%s stop failed: %s",
                    self._session_id,
                    exc,
                )
        await self._finish_task(task, "consume")
        await self._finish_task(workflow_task, "workflow drain")

    async def handle_event(self, event: Any) -> list[JournalEntry]:
        """Fold one monitor event and scan when it carries coordination state."""
        if not is_coordination_event(event):
            return []
        fold_monitor_event(self._snapshot, event)
        return await self.scan()

    async def scan(self) -> list[JournalEntry]:
        """Refresh native rows, detect conflicts, and apply the strategy once."""
        leader, tasks, members = await self._source.load()
        install_native_rows(
            self._snapshot,
            leader_member_name=leader,
            tasks=tasks,
            members=members,
        )
        conflicts = detect_conflicts(self._snapshot)
        entries: list[JournalEntry] = []
        reset_done: set[str] = set()
        for conflict in conflicts:
            entry = await self._recover(conflict, reset_done)
            if entry is not None:
                entries.append(entry)
        self._snapshot.previous_status = {
            task_id: task.status for task_id, task in self._snapshot.tasks.items()
        }
        return entries

    async def _consume(self) -> None:
        try:
            await self.scan()
        except Exception as exc:
            logger.warning(
                "teampulse.conflict session_id=%s initial scan failed: %s",
                self._session_id,
                exc,
            )
        if self._monitor is None:
            return
        try:
            async for event in self._monitor.events():
                try:
                    await self.handle_event(event)
                except Exception as exc:
                    logger.warning(
                        "teampulse.conflict session_id=%s event handling failed: %s",
                        self._session_id,
                        exc,
                    )
        except Exception as exc:
            logger.warning(
                "teampulse.conflict session_id=%s event stream failed: %s",
                self._session_id,
                exc,
            )

    async def _discard_workflow_events(self) -> None:
        """Read the public workflow stream so its queue does not grow unused."""
        monitor = self._monitor
        if monitor is None:
            return
        stream = getattr(monitor, "workflow_events", None)
        if not callable(stream):
            return
        try:
            async for _event in stream():
                continue
        except Exception as exc:
            logger.warning(
                "teampulse.conflict session_id=%s workflow drain failed: %s",
                self._session_id,
                exc,
            )

    async def _finish_task(self, task: asyncio.Task | None, label: str) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "teampulse.conflict session_id=%s %s stop failed: %s",
                self._session_id,
                label,
                exc,
            )

    async def _recover(self, conflict: Conflict, reset_done: set[str]) -> JournalEntry | None:
        fingerprint = conflict_fingerprint(conflict)
        if fingerprint in self._applied:
            return None
        decision = self._strategy.propose(conflict, self._snapshot)
        applied = False
        if decision.kind is DecisionKind.APPLY:
            applied, decision = await self._apply_resets(conflict, decision, reset_done)
        elif decision.kind is DecisionKind.RECONCILE:
            for task_id in conflict.task_ids:
                adopt_recorded_assignee(self._snapshot, task_id)
            applied = True
            self._log_recovery(conflict, decision)
        else:
            self._log_conflict(conflict, decision)
        self._applied.add(fingerprint)
        entry = JournalEntry(conflict=conflict, decision=decision, applied=applied)
        self._remember(entry)
        return entry

    async def _apply_resets(
        self,
        conflict: Conflict,
        decision: Decision,
        reset_done: set[str],
    ) -> tuple[bool, Decision]:
        applied_ids: list[str] = []
        failed = False
        for task_id in decision.reset_task_ids:
            if task_id in reset_done:
                applied_ids.append(task_id)
                continue
            try:
                ok = await self._source.reset_task(task_id)
            except Exception as exc:
                logger.warning(
                    "teampulse.recovery session_id=%s task_id=%s reset failed: %s",
                    self._session_id,
                    task_id,
                    exc,
                )
                ok = False
            if ok:
                reset_done.add(task_id)
                applied_ids.append(task_id)
            else:
                failed = True
        if failed or not applied_ids:
            escalated = Decision(
                kind=DecisionKind.ESCALATE,
                reason="Reset was rejected; leaving the conflict for the leader.",
            )
            self._log_conflict(conflict, escalated)
            return False, escalated
        applied = Decision(
            kind=DecisionKind.APPLY,
            reset_task_ids=tuple(applied_ids),
            reason=decision.reason,
        )
        self._log_recovery(conflict, applied)
        return True, applied

    def _remember(self, entry: JournalEntry) -> None:
        self._journal.append(entry)
        if len(self._journal) > _JOURNAL_LIMIT:
            del self._journal[: len(self._journal) - _JOURNAL_LIMIT]

    def _log_conflict(self, conflict: Conflict, decision: Decision) -> None:
        logger.warning(
            "teampulse.conflict session_id=%s team=%s type=%s tasks=%s members=%s "
            "impact=%s invariant=%s reason=%s",
            self._session_id,
            self._team_name,
            conflict.conflict_type,
            ",".join(conflict.task_ids),
            ",".join(conflict.member_names),
            conflict.impact.value,
            conflict.invariant,
            decision.reason,
        )

    def _log_recovery(self, conflict: Conflict, decision: Decision) -> None:
        logger.warning(
            "teampulse.recovery session_id=%s team=%s type=%s action=%s tasks=%s reason=%s",
            self._session_id,
            self._team_name,
            conflict.conflict_type,
            decision.kind.value,
            ",".join(decision.reset_task_ids or conflict.task_ids),
            decision.reason,
        )


def conflict_fingerprint(conflict: Conflict) -> str:
    """Stable identity of a conflict so a replay does not reset twice."""
    payload = {
        "type": conflict.conflict_type,
        "tasks": list(conflict.task_ids),
        "members": list(conflict.member_names),
        "event_type": conflict.evidence.event_type,
        "timestamp": conflict.evidence.timestamp,
        "previous": conflict.evidence.previous,
        "current": conflict.evidence.current,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _task_manager(monitor: Any) -> Any:
    agent = monitor_team_agent(monitor)
    if agent is None:
        return None
    try:
        return agent.task_manager
    except Exception:
        return None


async def _task_fact(manager: Any, row: Any) -> TaskFact:
    detail = None
    if manager is not None:
        try:
            detail = await manager.get_task_detail(row.task_id)
        except Exception as exc:
            logger.warning(
                "teampulse.conflict task_id=%s detail read failed: %s",
                getattr(row, "task_id", ""),
                exc,
            )
    source = detail if detail is not None else row
    blocked = getattr(source, "blocked_by", ()) or ()
    return TaskFact(
        task_id=row.task_id,
        status=getattr(source, "status", None) or row.status,
        assignee=getattr(source, "assignee", None),
        updated_at=getattr(source, "updated_at", None),
        blocked_by=tuple(blocked),
        summary=getattr(source, "summary", None),
        note=getattr(source, "note", None),
    )


class _SessionBind:
    def __init__(self, monitor: Any) -> None:
        self._monitor = monitor
        self._token = None

    def __enter__(self) -> None:
        session_id = getattr(self._monitor, "session_id", None)
        if session_id and get_session_id() != session_id:
            self._token = set_session_id(session_id)

    def __exit__(self, *_) -> None:
        if self._token is not None:
            reset_session_id(self._token)
            self._token = None


def _bound_monitor_session(monitor: Any) -> _SessionBind:
    return _SessionBind(monitor)
