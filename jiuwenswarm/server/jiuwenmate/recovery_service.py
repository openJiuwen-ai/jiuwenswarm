"""Restart scan of the Requests a previous process left dispatched.

Each one is judged from its latest Execution's saved log:

- a saved final or paused event decides the outcome and is not run again;
- otherwise a stop or cancel the sender gave decides it (JM-12), and such a
  Request is never continued;
- otherwise the Execution was interrupted: the judgment is awaiting continuation,
  and the same Request is queued again with ``recovery_ref`` pointing at that
  Execution. Confirmation resume is not this path.

``tool.completed`` is written in the background, so an intent without a saved
completion is recorded as unverified, not as a tool that did not run. Every
judgment is stored once per Execution; a later scan reuses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from jiuwenswarm.server.jiuwenmate.execution_control_service import RequestControl
from jiuwenswarm.server.jiuwenmate.execution_driver import outcome_of
from jiuwenswarm.server.jiuwenmate.execution_models import RequestState
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.session_scheduler import SessionScheduler
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore
from openjiuwen.harness import persistent_agent as core

logger = logging.getLogger(__name__)

RecoveryDecision = Literal["closed_from_log", "closed_by_control", "awaiting_continuation"]
_TERMINAL = frozenset({core.EventType.EXECUTION_FINAL, core.EventType.EXECUTION_PAUSED})
_CONTROL_OUTCOMES = {"stop": "stopped", "cancel": "cancelled"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_recoveries (
    namespace_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    unverified_tool_calls TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, request_id, execution_id)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, kw_only=True)
class RequestRecovery:
    """How a Request left by a previous process was closed; ``execution_id`` is the attempt a continuation resumes."""

    request_id: str
    execution_id: str
    decision: RecoveryDecision
    outcome: str
    unverified_tool_calls: tuple[str, ...]
    decided_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "decision": self.decision,
            "outcome": self.outcome,
            "unverified_tool_calls": list(self.unverified_tool_calls),
            "decided_at": self.decided_at.isoformat(),
        }


class RecoveryStore(SQLiteStore):
    _schema = _SCHEMA

    @staticmethod
    def _recovery(row) -> RequestRecovery:
        return RequestRecovery(
            request_id=row["request_id"],
            execution_id=row["execution_id"],
            decision=row["decision"],
            outcome=row["outcome"],
            unverified_tool_calls=tuple(json.loads(row["unverified_tool_calls"])),
            decided_at=datetime.fromisoformat(row["decided_at"]),
        )

    def claim(self, namespace_id: str, recovery: RequestRecovery) -> RequestRecovery:
        """Store the judgment of an Execution; one stored earlier is returned instead."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO request_recoveries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace_id,
                    recovery.request_id,
                    recovery.execution_id,
                    recovery.decision,
                    recovery.outcome,
                    json.dumps(list(recovery.unverified_tool_calls)),
                    recovery.decided_at.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM request_recoveries WHERE namespace_id = ? AND request_id = ? AND execution_id = ?",
                (namespace_id, recovery.request_id, recovery.execution_id),
            ).fetchone()
        return self._recovery(row)

    def latest(self, namespace_id: str, request_id: str) -> RequestRecovery | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM request_recoveries WHERE namespace_id = ? AND request_id = ? "
                "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                (namespace_id, request_id),
            ).fetchone()
        return None if row is None else self._recovery(row)


def unverified_tool_calls(log) -> tuple[str, ...]:
    """Tool calls whose intent was saved but whose completion was not."""
    completed = {event.payload.tool_call_id for event in log if event.event_type == core.EventType.TOOL_COMPLETED}
    return tuple(
        event.payload.tool_call_id
        for event in log
        if event.event_type == core.EventType.TOOL_INTENT and event.payload.tool_call_id not in completed
    )


class RecoveryService:
    def __init__(
        self,
        sessions: ExecutionRepository,
        scheduler: SessionScheduler,
        recoveries: RecoveryStore,
        *,
        control_of: Callable[[str, str], RequestControl | None],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._scheduler = scheduler
        self._recoveries = recoveries
        self._control_of = control_of
        self._clock = clock

    async def recover(
        self,
        namespace_id: str,
        *,
        channels: frozenset[str] | None = None,
        include_unrouted: bool = True,
    ) -> list[RequestRecovery]:
        """Queue waiting Requests again and close the dispatched ones left behind; their Sessions move on."""
        left = await self._scheduler.recover(
            namespace_id, channels=channels, include_unrouted=include_unrouted,
        )
        closed = []
        for state in left:
            try:
                recovery = await asyncio.to_thread(self._close, namespace_id, state)
            except Exception as exc:
                logger.warning(
                    "[JiuwenMate] left request not recovered: request=%s error=%s", state.record.request_id, exc
                )
                continue
            if recovery is not None:
                closed.append(recovery)
            await self._scheduler.wake(namespace_id, state.record.session_id)
        return closed

    def recovery_of(self, namespace_id: str, request_id: str) -> RequestRecovery | None:
        return self._recoveries.latest(namespace_id, request_id)

    def _close(self, namespace_id: str, state: RequestState) -> RequestRecovery | None:
        record = state.record
        executions = self._sessions.executions(namespace_id, record.request_id)
        if not executions:
            logger.warning("[JiuwenMate] left request has no execution: request=%s", record.request_id)
            self._sessions.finish_request(namespace_id, record.request_id, "failed", now=self._clock())
            return None
        latest = executions[-1]
        log = self._sessions.read_execution_log(namespace_id, record.session_id, record.request_id, latest.execution_id)
        recovery = self._recoveries.claim(namespace_id, self._judge(namespace_id, record.request_id, latest, log))
        self._sessions.finish_request(namespace_id, record.request_id, recovery.outcome, now=self._clock())
        if recovery.decision == "awaiting_continuation":
            self._sessions.reopen_for_continuation(namespace_id, record.request_id, now=self._clock())
        logger.info(
            "[JiuwenMate] left request closed: request=%s decision=%s outcome=%s unverified=%s",
            record.request_id,
            recovery.decision,
            recovery.outcome,
            len(recovery.unverified_tool_calls),
        )
        return recovery

    def _judge(self, namespace_id: str, request_id: str, execution, log) -> RequestRecovery:
        terminal = next((event for event in log if event.event_type in _TERMINAL), None)
        control = self._control_of(namespace_id, request_id)
        if terminal is not None:
            decision, outcome = "closed_from_log", outcome_of(terminal)
        elif control is not None:
            decision, outcome = "closed_by_control", _CONTROL_OUTCOMES[control.reason]
        else:
            decision, outcome = "awaiting_continuation", "paused"
        return RequestRecovery(
            request_id=request_id,
            execution_id=execution.execution_id,
            decision=decision,
            outcome=outcome,
            unverified_tool_calls=unverified_tool_calls(log),
            decided_at=self._clock(),
        )
