"""Lifecycle of the JiuwenMate runtime: ports, scheduler and periodic window cleanup.

``AgentManager`` assembles the host ports shared by every Execution, owns the
``SessionScheduler`` and runs window cleanup in the background. It is started
and closed together with its host; deleting a colleague goes through it so the
colleague's work has ended before its data is removed. Private messages enter
through it, and its request views show only replies a final event adopted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_control_service import (
    ControlStore,
    ExecutionControlService,
    RequestControl,
)
from jiuwenswarm.server.jiuwenmate.execution_driver import (
    AbortEntry,
    ExecuteEntry,
    ExecutionDriver,
    _default_abort,
    _default_execute,
)
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, RequestState
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.budget import DEFAULT_RESERVATION_TOKENS, HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.recovery_service import RecoveryService, RecoveryStore, RequestRecovery
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService, default_data_dir
from jiuwenswarm.server.jiuwenmate.session_scheduler import SchedulerLimits, SessionScheduler
from openjiuwen.harness import persistent_agent as core

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_RETENTION = timedelta(days=90)
DEFAULT_PURGE_INTERVAL = timedelta(hours=1)
_TERMINAL_EVENTS = frozenset({core.EventType.EXECUTION_FINAL, core.EventType.EXECUTION_PAUSED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, kw_only=True)
class ReplyDraft:
    """Text the model is streaming in an unfinished Request; ``seq`` is the last event it includes.

    Each model call starts a new segment, so text written before a tool call does not run into the answer;
    ``closed`` means the model call of ``text`` has completed. It is only shown as work in progress:
    the reply is what the final event adopts.
    """

    execution_id: str
    seq: int
    text: str
    closed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, "seq": self.seq, "text": self.text, "closed": self.closed}


def reply_draft(execution_id: str, events) -> ReplyDraft | None:
    text, seq, closed = "", 0, False
    for event in events:
        if event.event_type == core.EventType.REPLY_DELTA:
            if closed:
                text, closed = "", False
            text += event.payload.text
            seq = event.local_seq
        elif event.event_type == core.EventType.MODEL_COMPLETED:
            closed = True
            seq = event.local_seq
    return ReplyDraft(execution_id=execution_id, seq=seq, text=text, closed=closed) if seq else None


@dataclass(frozen=True, kw_only=True)
class RequestView:
    """What a page may show about one Request; replies are the outputs its final event adopted."""

    state: RequestState
    running: bool
    replies: tuple[str, ...]
    reason: str | None
    draft: ReplyDraft | None = None
    conversation_key: str | None = None
    control: RequestControl | None = None
    recovery: RequestRecovery | None = None

    def to_dict(self) -> dict[str, Any]:
        record = self.state.record
        source = record.request_input.source
        return {
            "request_id": record.request_id,
            "session_id": record.session_id,
            "conversation_id": self.conversation_key,
            "agent_id": record.agent_id,
            "message_id": getattr(source, "message_id", None),
            "text": record.request_input.text,
            "received_at": record.received_at.isoformat(),
            "status": self.state.status,
            "outcome": self.state.outcome,
            "running": self.running,
            "replies": list(self.replies),
            "reason": self.reason,
            "draft": None if self.draft is None else self.draft.to_dict(),
            "control": None if self.control is None else self.control.to_dict(),
            "recovery": None if self.recovery is None else self.recovery.to_dict(),
            "updated_at": self.state.updated_at.isoformat(),
        }


class AgentManager:
    def __init__(
        self,
        service: PersistentAgentService,
        sessions: ExecutionRepository,
        windows: WindowBackendStore,
        budgets: BudgetRepository,
        *,
        execute: ExecuteEntry = _default_execute,
        abort: AbortEntry = _default_abort,
        limits: SchedulerLimits | None = None,
        reservation_tokens: int = DEFAULT_RESERVATION_TOKENS,
        archive_retention: timedelta = DEFAULT_ARCHIVE_RETENTION,
        purge_interval: timedelta = DEFAULT_PURGE_INTERVAL,
        clock: Callable[[], datetime] = _utc_now,
        on_event: Callable[[core.AgentEvent], None] | None = None,
    ) -> None:
        if purge_interval <= timedelta(0):
            raise ValueError("purge_interval must be positive")
        if archive_retention < timedelta(0):
            raise ValueError("archive_retention must not be negative")
        self._service = service
        self._sessions = sessions
        self._windows = windows
        self._archive_retention = archive_retention
        self._purge_interval = purge_interval
        self._clock = clock
        self._queue = CommitQueue(sessions, clock=clock)
        driver = ExecutionDriver(
            snapshots=service.snapshot,
            commit_port=HostCommitPort(sessions, self._queue, clock=clock, observer=on_event),
            queue=self._queue,
            budget_port=HostBudgetPort(budgets, sessions, reservation_tokens=reservation_tokens, clock=clock),
            budgets=budgets,
            windows=windows,
            workspaces=service.workspaces,
            execute=execute,
            abort=abort,
            clock=clock,
        )
        self._scheduler = SessionScheduler(sessions, driver, limits=limits, clock=clock)
        self._control = ExecutionControlService(sessions, self._scheduler, ControlStore(sessions.db_path), clock=clock)
        self._recovery = RecoveryService(
            sessions, self._scheduler, RecoveryStore(sessions.db_path), control_of=self._control.control_of, clock=clock
        )
        self._purge_task: asyncio.Task[None] | None = None

    @classmethod
    def create_default(
        cls,
        service: PersistentAgentService,
        data_dir: Path | None = None,
        *,
        on_event: Callable[[core.AgentEvent], None] | None = None,
    ) -> AgentManager:
        """Runtime over the same SQLite file as ``PersistentAgentService.create_default``."""
        db = (Path(data_dir) if data_dir is not None else default_data_dir()) / "agents.sqlite3"
        return cls(service, ExecutionRepository(db), WindowBackendStore(db), BudgetRepository(db), on_event=on_event)

    @property
    def scheduler(self) -> SessionScheduler:
        return self._scheduler

    @property
    def commit_queue(self) -> CommitQueue:
        return self._queue

    async def start(self) -> None:
        if self._purge_task is not None:
            return
        await self._queue.start()
        self._scheduler.start()
        self._purge_task = asyncio.create_task(self._purge_loop(), name="jiuwenmate-window-purge")

    async def aclose(self) -> None:
        """Stop every Execution, wait for their streams to end, then stop cleanup and the commit queue."""
        await self._scheduler.aclose()
        if self._purge_task is not None:
            self._purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._purge_task
            self._purge_task = None
        await self._queue.aclose()

    async def delete_colleague(self, namespace_id: str, agent_id: str) -> None:
        """End the colleague's queued and running work, then remove the colleague and its window data."""
        await self._scheduler.end_agent(namespace_id, agent_id)
        try:
            await asyncio.to_thread(self._service.delete_colleague, namespace_id, agent_id)
        finally:
            self._scheduler.release_agent(namespace_id, agent_id)

    async def submit_direct_message(
        self,
        namespace_id: str,
        agent_id: str,
        conversation: ConversationRef,
        *,
        message_id: str,
        text: str,
        access: AccessSnapshot,
        sender_name: str,
    ) -> RequestView:
        """Queue a private message pinned to the current version; resending a message_id returns its Request."""
        snapshot = await asyncio.to_thread(self._service.current_snapshot, namespace_id, agent_id)
        now = self._clock()
        try:
            request_input = core.RequestInput(
                kind="direct_message",
                text=text,
                source=core.DirectMessageSource(
                    message_id=message_id, sender_id=access.principal_ref, sender_name=sender_name, received_at=now
                ),
                reply_target=conversation.key,
            )
        except ValidationError as exc:
            raise PersistentAgentError(f"invalid message: {exc.errors()[0]['msg']}") from exc
        session = await asyncio.to_thread(self._sessions.resolve_session, namespace_id, agent_id, conversation, now=now)
        state, _ = await self._scheduler.accept(
            session,
            snapshot=snapshot,
            source_ref=f"{conversation.channel}:{message_id}",
            access=access,
            request_input=request_input,
            received_at=now,
        )
        return await self.request_view(namespace_id, state.record.request_id)

    async def conversation_requests(
        self, namespace_id: str, agent_id: str, conversation: ConversationRef
    ) -> list[RequestView]:
        """Requests of the colleague's Session in this conversation, oldest first; empty before first contact."""
        session = await asyncio.to_thread(self._sessions.find_session, namespace_id, agent_id, conversation)
        if session is None:
            return []
        states = await asyncio.to_thread(self._sessions.session_requests, namespace_id, session.session_id)
        return [await self._view(state, conversation.key) for state in states]

    async def request_view(self, namespace_id: str, request_id: str) -> RequestView:
        state = await asyncio.to_thread(self._sessions.get_request, namespace_id, request_id)
        session = await asyncio.to_thread(self._sessions.get_session, namespace_id, state.record.session_id)
        return await self._view(state, session.conversation.key)

    async def recover(self, namespace_id: str) -> list[RequestRecovery]:
        """Resume scheduling after a restart: queued Requests run again, ones left dispatched are closed."""
        return await self._recovery.recover(namespace_id)

    async def control_request(
        self, namespace_id: str, request_id: str, *, reason: str, access: AccessSnapshot
    ) -> RequestView:
        """Stop or cancel the Request on behalf of ``access``; the view carries the stored control."""
        await self._control.control(namespace_id, request_id, reason=reason, principal_ref=access.principal_ref)
        return await self.request_view(namespace_id, request_id)

    @property
    def db_path(self) -> Path:
        """The SQLite file of this runtime; host tables kept beside the Sessions use it too."""
        return self._sessions.db_path

    async def _view(self, state: RequestState, conversation_key: str) -> RequestView:
        # The scheduler's running set changes on the event loop, so it is read here and not in the thread.
        running = any(run.record.request_id == state.record.request_id for run in self._scheduler.running)
        replies, reason, draft = await asyncio.to_thread(self._result, state)
        control = await asyncio.to_thread(self._control.control_of, state.record.namespace_id, state.record.request_id)
        recovery = await asyncio.to_thread(
            self._recovery.recovery_of, state.record.namespace_id, state.record.request_id
        )
        return RequestView(
            state=state,
            running=running,
            replies=replies,
            reason=reason,
            draft=draft,
            conversation_key=conversation_key,
            control=control,
            recovery=recovery,
        )

    def _result(self, state: RequestState) -> tuple[tuple[str, ...], str | None, ReplyDraft | None]:
        """Outcome of the latest Execution: adopted replies and reason once it ended, else its draft."""
        record = state.record
        executions = self._sessions.executions(record.namespace_id, record.request_id)
        if not executions:
            return (), None, None
        latest = executions[-1]
        log = self._sessions.read_execution_log(
            record.namespace_id, record.session_id, record.request_id, latest.execution_id
        )
        terminal = next((event for event in log if event.event_type in _TERMINAL_EVENTS), None)
        if terminal is None:
            return (), None, None if state.status == "ended" else reply_draft(latest.execution_id, log)
        payload = terminal.payload
        if terminal.event_type == core.EventType.EXECUTION_PAUSED:
            return (), payload.detail, None
        replies = []
        for output_id in payload.output_refs:
            output = self._sessions.candidate_output(record.namespace_id, output_id)
            if output is not None and output.payload.share_mode == "message" and output.payload.text:
                replies.append(output.payload.text)
        return tuple(replies), payload.reason, None

    async def _purge_loop(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(
                    self._windows.purge_expired, archive_retention=self._archive_retention
                )
                if removed:
                    logger.info("[JiuwenMate] window entries purged: %s", removed)
            except Exception as exc:
                logger.warning("[JiuwenMate] window purge failed: %s", exc)
            await asyncio.sleep(self._purge_interval.total_seconds())
