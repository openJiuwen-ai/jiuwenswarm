"""Lifecycle of the JiuwenMate runtime: ports, scheduler and periodic window cleanup.

``AgentManager`` assembles the host ports shared by every Execution, owns the
``SessionScheduler`` and runs window cleanup in the background. It is started
and closed together with its host; deleting a colleague goes through it so the
colleague's work has ended before its data is removed. Private messages enter
through it, and its request views show only the replies and files a final or
paused event handed off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime
from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import (
    AgentMailRuntimeService,
    AgentMailRuntimeStore,
    AgentMailScope,
    load_agent_mail_runtime_provider,
)
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.ports.group_chat import unavailable_membership
from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
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
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    RequestRouteSnapshot,
    RequestState,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.output_files import FILE_SHARE_MODES, WorkspaceOutputFiles
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import (
    PermissionApproval,
    PermissionApprovalRepository,
)
from jiuwenswarm.server.jiuwenmate.ports.budget import DEFAULT_RESERVATION_TOKENS, HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.recovery_service import (
    RecoveryService,
    RecoveryStore,
    RequestRecovery,
    unverified_tool_calls,
)
from jiuwenswarm.server.jiuwenmate.scheduled_requests import DEFAULT_SCHEDULE_INTERVAL, ScheduleService
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService, default_data_dir
from jiuwenswarm.server.jiuwenmate.session_scheduler import SchedulerLimits, SessionScheduler
from openjiuwen.harness import persistent_agent as core

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_RETENTION = timedelta(days=90)
DEFAULT_PURGE_INTERVAL = timedelta(hours=1)
_TERMINAL_EVENTS = frozenset({core.EventType.EXECUTION_FINAL, core.EventType.EXECUTION_PAUSED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log_thinking_notice(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[JiuwenMate] thinking notice failed: %s", type(exc).__name__)


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
    """What a page may show about one Request; replies and files are the outputs its ending handed off.

    A file is described by its name, size and caption and downloaded through a signed
    link to the verified file in the colleague's shared output directory; no office path is shown.
    """

    state: RequestState
    running: bool
    replies: tuple[str, ...]
    reason: str | None
    draft: ReplyDraft | None = None
    conversation_key: str | None = None
    control: RequestControl | None = None
    deliveries: tuple[dict[str, object], ...] = ()
    recovery: RequestRecovery | None = None
    files: tuple[dict[str, object], ...] = ()
    approval: dict[str, Any] | None = None

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
            "files": [dict(item) for item in self.files],
            "approval": None if self.approval is None else dict(self.approval),
            "reason": self.reason,
            "draft": None if self.draft is None else self.draft.to_dict(),
            "control": None if self.control is None else self.control.to_dict(),
            "deliveries": [
                {
                    key: delivery.get(key)
                    for key in ("output_id", "channel", "status", "external_message_id", "error_code")
                }
                for delivery in self.deliveries
            ],
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
        delivery_service: ResultDeliveryService | None = None,
        thinking_notice_sender: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        files: WorkspaceOutputFiles | None = None,
        schedule_interval: timedelta = DEFAULT_SCHEDULE_INTERVAL,
        cli: FeishuCliRuntime | None = None,
        agent_mail_runtime: AgentMailRuntimeService | None = None,
        permission_approvals: PermissionApprovalRepository | None = None,
    ) -> None:
        if purge_interval <= timedelta(0) or schedule_interval <= timedelta(0):
            raise ValueError("purge_interval and schedule_interval must be positive")
        if archive_retention < timedelta(0):
            raise ValueError("archive_retention must not be negative")
        self._service = service
        self.cli_runtime = cli or getattr(service, "cli_runtime", None) or FeishuCliRuntime(sessions.db_path.parent)
        self.agent_mail_runtime = agent_mail_runtime or AgentMailRuntimeService(
            store=AgentMailRuntimeStore(sessions.db_path), runtime=load_agent_mail_runtime_provider()
        )
        self._cli_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._cli_requested: set[tuple[str, str]] = set()
        self._cli_slots = asyncio.Semaphore(2)
        self._cli_closing = False
        self._sessions = sessions
        self._windows = windows
        self._archive_retention = archive_retention
        self._purge_interval = purge_interval
        self._schedule_interval = schedule_interval
        self._clock = clock
        self._external_event_observer = on_event
        self._delivery_service = delivery_service
        self._thinking_notice_sender = thinking_notice_sender
        self._thinking_notices_sent: set[str] = set()
        self._permission_approvals = permission_approvals or PermissionApprovalRepository(sessions.db_path)
        self._permission_resume_tasks: dict[str, asyncio.Task] = {}
        self.group_chats = GroupChatRepository(sessions.db_path)
        self.group_archive = ConversationArchive(sessions.db_path.parent / "group_messages", self.group_chats)
        if self._delivery_service is not None:
            self._delivery_service.set_mention_roster(self._delivery_mention_roster)
        self.verify_group_membership = unavailable_membership
        self.refresh_group_messages = None
        self._files = files or WorkspaceOutputFiles(service.workspaces)
        self._queue = CommitQueue(sessions, clock=clock)
        driver = ExecutionDriver(
            snapshots=service.snapshot,
            commit_port=HostCommitPort(
                sessions,
                self._queue,
                clock=clock,
                observer=self._observe_event,
                files=self._files,
            ),
            queue=self._queue,
            budget_port=HostBudgetPort(budgets, sessions, reservation_tokens=reservation_tokens, clock=clock),
            budgets=budgets,
            windows=windows,
            workspaces=service.workspaces,
            groups=self.group_chats,
            group_archive=self.group_archive,
            sessions=sessions,
            verify_group_membership=self._verify_group_membership,
            refresh_group_messages=self._refresh_group_messages,
            group_roster=self.group_collaboration_roster,
            execute=execute,
            abort=abort,
            clock=clock,
            cli=self.cli_runtime,
            agent_mail_runtime=self.agent_mail_runtime,
            permission_approvals=self._permission_approvals,
        )
        self._scheduler = SessionScheduler(
            sessions,
            driver,
            limits=limits,
            clock=clock,
            has_pending_approval=self._permission_approvals.has_pending_session,
            is_ready_approval=self._permission_approvals.is_ready_for_request,
        )
        self._control = ExecutionControlService(sessions, self._scheduler, ControlStore(sessions.db_path), clock=clock)
        self._recoveries = RecoveryStore(sessions.db_path)
        self._recovery = RecoveryService(
            sessions, self._scheduler, self._recoveries, control_of=self._control.control_of, clock=clock
        )
        self._schedules = ScheduleService(
            sessions,
            self._scheduler,
            snapshots=service.current_snapshot,
            colleagues=service.get_colleague,
            clock=clock,
        )
        self._purge_task: asyncio.Task[None] | None = None
        self._schedule_task: asyncio.Task[None] | None = None
        self._permission_task: asyncio.Task[None] | None = None

    def group_participant_roster(self, namespace_id: str, group_id: str) -> list[dict[str, Any]]:
        """Return every published colleague with an active Bot binding in this group."""
        return self._group_roster(namespace_id, group_id, exclude_agent_id=None)

    def group_collaboration_roster(
        self,
        namespace_id: str,
        group_id: str,
        sender_agent_id: str,
    ) -> list[dict[str, Any]]:
        """Return published colleagues with an active Bot binding in this group only."""
        return self._group_roster(namespace_id, group_id, exclude_agent_id=sender_agent_id)

    def _group_roster(
        self,
        namespace_id: str,
        group_id: str,
        *,
        exclude_agent_id: str | None,
    ) -> list[dict[str, Any]]:
        active_apps: dict[str, set[str]] = {}
        for access in self.group_chats.active_group_members(namespace_id, group_id):
            if access.agent_id != exclude_agent_id:
                active_apps.setdefault(access.agent_id, set()).add(access.app_id)
        return [
            {
                "agent_id": colleague.agent_id,
                "name": colleague.name,
                "app_ids": sorted(active_apps[colleague.agent_id]),
            }
            # Group members are the agents of IM connections, each named after its colleague.
            for colleague in self._service.list_agents(namespace_id)
            if colleague.agent_id in active_apps and colleague.enabled and colleague.current_version is not None
        ]

    def _delivery_mention_roster(self, job: dict[str, object]) -> list[dict[str, Any]]:
        if job.get("request_kind") != "group_message":
            return []
        namespace_id = job.get("namespace_id")
        session_id = job.get("session_id")
        sender_agent_id = job.get("agent_id")
        if not all(isinstance(value, str) and value for value in (namespace_id, session_id, sender_agent_id)):
            return []
        session = self._sessions.get_session(namespace_id, session_id)
        if session.conversation.kind != "group":
            return []
        return self.group_collaboration_roster(namespace_id, session.conversation.key, sender_agent_id)

    async def _verify_group_membership(self, access, group):
        return await self.verify_group_membership(access, group)

    async def _refresh_group_messages(self, access, group, messages):
        if self.refresh_group_messages is None:
            return messages
        return await self.refresh_group_messages(access, group, messages)

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

    @property
    def result_delivery_service(self) -> ResultDeliveryService | None:
        return self._delivery_service

    async def start(self) -> None:
        if self._purge_task is not None:
            return
        await self._queue.start()
        if self._delivery_service is not None:
            await self._delivery_service.start()
        self._scheduler.start()
        self._purge_task = asyncio.create_task(self._purge_loop(), name="jiuwenmate-window-purge")
        self._schedule_task = asyncio.create_task(self._schedule_loop(), name="jiuwenmate-schedules")
        self._permission_task = asyncio.create_task(
            self._permission_approval_loop(), name="jiuwenmate-permission-approvals"
        )
        await self._recover_permission_approvals()
        agents = set(self.cli_runtime.bound_agents())
        list_agents = getattr(self._service, "list_agents", None)
        if list_agents is not None:
            # Reconcile removed bindings too, without publishing an owner's pending draft.
            colleagues = await asyncio.to_thread(list_agents, "local")
            agents.update(("local", item.agent_id) for item in colleagues if item.current_version is not None)
        for namespace_id, agent_id in agents:
            self.schedule_feishu_cli(namespace_id, agent_id)

    def schedule_feishu_cli(self, namespace_id: str, agent_id: str) -> dict[str, object]:
        """Enqueue optional setup; never hold an IM/Web request open during npm installation."""
        key = namespace_id, agent_id
        if self._cli_closing:
            return {"status": "pending"}
        if key in self._cli_tasks:
            self._cli_requested.add(key)
        elif len(self._cli_tasks) < 64:
            self._cli_tasks[key] = asyncio.create_task(self._prepare_cli_loop(key), name="jiuwenmate-feishu-cli")
        return {"status": "pending"}

    async def _prepare_cli_loop(self, key: tuple[str, str]) -> None:
        try:
            while not self._cli_closing:
                self._cli_requested.discard(key)
                await self.prepare_feishu_cli(*key)
                if key not in self._cli_requested:
                    break
        finally:
            # No callback gap: a new notification after this point creates a new worker.
            self._cli_tasks.pop(key, None)
            self._cli_requested.discard(key)

    async def prepare_feishu_cli(self, namespace_id: str, agent_id: str) -> dict[str, object]:
        async with self._cli_slots:
            try:
                await asyncio.to_thread(self._service.get_colleague, namespace_id, agent_id)
                result = await self.cli_runtime.prepare(namespace_id, agent_id)
                directories = self.cli_runtime.skill_directories(namespace_id, agent_id)
                # Even unbinding/failure removes stale identity claims for new Requests.
                published = await asyncio.to_thread(self._service.refresh_feishu_capability, namespace_id,
                                                     agent_id, skill_directories=directories)
                return {"status": result.get("status"), "publication": "ready" if published else "not_published"}
            except Exception:
                return {"status": "unavailable", "publication": "pending"}

    async def aclose(self) -> None:
        """Stop every Execution, wait for their streams to end, then stop cleanup and the commit queue."""
        self._cli_closing = True
        tasks = tuple(self._cli_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cli_tasks.clear()
        self._cli_requested.clear()
        if self._permission_task is not None:
            self._permission_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._permission_task
            self._permission_task = None
        resume_tasks = tuple(self._permission_resume_tasks.values())
        for task in resume_tasks:
            task.cancel()
        await asyncio.gather(*resume_tasks, return_exceptions=True)
        self._permission_resume_tasks.clear()
        await self._scheduler.aclose()
        if self._delivery_service is not None:
            await self._delivery_service.aclose()
        if self._purge_task is not None:
            self._purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._purge_task
            self._purge_task = None
        if self._schedule_task is not None:
            self._schedule_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._schedule_task
            self._schedule_task = None
        await self._queue.aclose()

    async def delete_colleague(self, namespace_id: str, agent_id: str, *, record_removal: bool = False) -> None:
        """End the colleague's queued and running work, then remove the colleague and its window data.

        The colleague's rows go last, so after any failure it is still found and deleted again.
        """
        await self._scheduler.end_agent(namespace_id, agent_id)
        preparing = self._cli_tasks.get((namespace_id, agent_id))
        if preparing is not None:
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        self._cli_tasks.pop((namespace_id, agent_id), None)
        self._cli_requested.discard((namespace_id, agent_id))
        await asyncio.to_thread(self._schedules.delete_agent, namespace_id, agent_id)
        try:
            await self.cli_runtime.remove_agent(namespace_id, agent_id)
            await self.agent_mail_runtime.remove(AgentMailScope(namespace_id, agent_id))
            await asyncio.to_thread(
                self._service.delete_colleague, namespace_id, agent_id, record_removal=record_removal,
            )
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
            route_snapshot=(
                RequestRouteSnapshot(channel="web", target_id=conversation.key, source_message_id=message_id)
                if conversation.kind == "web_direct"
                else None
            ),
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

    async def latest_conversation_message(
        self, namespace_id: str, agent_id: str, conversation: ConversationRef
    ) -> str:
        """Return the latest user message without materializing the full conversation history."""
        session = await asyncio.to_thread(self._sessions.find_session, namespace_id, agent_id, conversation)
        if session is None:
            return ""
        state = await asyncio.to_thread(self._sessions.latest_session_request, namespace_id, session.session_id)
        return "" if state is None else state.record.request_input.text

    async def request_view(self, namespace_id: str, request_id: str) -> RequestView:
        state = await asyncio.to_thread(self._sessions.get_request, namespace_id, request_id)
        session = await asyncio.to_thread(self._sessions.get_session, namespace_id, state.record.session_id)
        return await self._view(state, session.conversation.key)

    async def decide_permission_approval(
        self,
        approval_id: str,
        *,
        decision: Literal["approve", "reject"],
        access: AccessSnapshot,
        channel: str,
        target_id: str,
        app_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> RequestView:
        """Accept a confirmation only from the original initiator and route, then resume its Request."""
        approval = await asyncio.to_thread(
            self._permission_approvals.decide,
            approval_id,
            decision=decision,
            principal_ref=access.principal_ref,
            channel=channel,
            target_id=target_id,
            app_id=app_id,
            tool_call_id=tool_call_id,
            now=self._clock(),
        )
        self._schedule_permission_resume(approval)
        return await self.request_view(approval.namespace_id, approval.request_id)

    async def handle_feishu_permission_reply(
        self,
        text: str,
        *,
        access: AccessSnapshot,
        target_id: str,
        app_id: str,
    ) -> bool:
        """Consume an explicit Feishu approval command before it can become a new Agent Request."""
        match = re.fullmatch(
            r"\s*(approve|批准|同意|reject|拒绝)\s+([a-f0-9]{12})(?:\s+([^\s]+))?\s*",
            text,
            re.IGNORECASE,
        )
        if match is None:
            return False
        decision = "approve" if match.group(1).lower() in ("approve", "批准", "同意") else "reject"
        tool_call_id = match.group(3)
        if (decision == "approve") != (tool_call_id is not None):
            return True
        try:
            await self.decide_permission_approval(
                match.group(2),
                decision=decision,
                access=access,
                channel="feishu",
                target_id=target_id,
                app_id=app_id,
                tool_call_id=tool_call_id,
            )
        except PersistentAgentError as exc:
            logger.info("[JiuwenMate] Feishu permission reply was refused: code=%s", exc.code)
        return True

    async def recover(
        self,
        namespace_id: str,
        *,
        channels: frozenset[str] | None = None,
        include_unrouted: bool = True,
    ) -> list[RequestRecovery]:
        """Resume scheduling after a restart: queued Requests run again, ones left dispatched are closed."""
        await self._recover_permission_approvals()
        return await self._recovery.recover(
            namespace_id, channels=channels, include_unrouted=include_unrouted,
        )

    async def control_request(
        self, namespace_id: str, request_id: str, *, reason: str, access: AccessSnapshot
    ) -> RequestView:
        """Stop or cancel the Request on behalf of ``access``; the view carries the stored control."""
        await self._control.control(namespace_id, request_id, reason=reason, principal_ref=access.principal_ref)
        return await self.request_view(namespace_id, request_id)

    @property
    def sessions(self) -> ExecutionRepository:
        """Request and execution records. Group traces read only this colleague's Session."""
        return self._sessions

    @property
    def db_path(self) -> Path:
        """The SQLite file of this runtime; host tables kept beside the Sessions use it too."""
        return self._sessions.db_path

    async def _view(self, state: RequestState, conversation_key: str) -> RequestView:
        # The scheduler's running set changes on the event loop, so it is read here and not in the thread.
        running = any(run.record.request_id == state.record.request_id for run in self._scheduler.running)
        replies, files, reason, draft = await asyncio.to_thread(self._result, state)
        control = await asyncio.to_thread(self._control.control_of, state.record.namespace_id, state.record.request_id)
        deliveries = await asyncio.to_thread(
            self._sessions.result_deliveries, state.record.namespace_id, state.record.request_id
        )
        status_of = {delivery["output_id"]: delivery["status"] for delivery in deliveries}
        files = tuple({**item, "delivery_status": status_of.get(item["output_id"])} for item in files)
        recovery = await asyncio.to_thread(
            self._recovery.recovery_of, state.record.namespace_id, state.record.request_id
        )
        approval = await asyncio.to_thread(
            self._permission_approvals.pending_for_request, state.record.namespace_id, state.record.request_id
        )
        return RequestView(
            state=state,
            running=running,
            replies=replies,
            reason=reason,
            draft=draft,
            conversation_key=conversation_key,
            control=control,
            deliveries=tuple(deliveries),
            recovery=recovery,
            files=files,
            approval=None if approval is None else approval.to_view(),
        )

    def _observe_event(self, event: core.AgentEvent) -> None:
        try:
            self._observe_permission_approval(event)
            if self._external_event_observer is not None:
                self._external_event_observer(event)
        finally:
            if self._delivery_service is not None:
                self._delivery_service.observe(event)
            self._schedule_thinking_notice(event)

    def _observe_permission_approval(self, event: core.AgentEvent) -> None:
        if event.event_type != core.EventType.EXECUTION_PAUSED or event.payload.reason != "awaiting_confirmation":
            return
        try:
            state = self._sessions.get_request(event.namespace_id, event.request_id)
            execution = next(
                item
                for item in self._sessions.executions(event.namespace_id, event.request_id)
                if item.execution_id == event.execution_id
            )
            log = self._sessions.read_execution_log(
                event.namespace_id, state.record.session_id, event.request_id, event.execution_id
            )
            self._recoveries.claim(
                event.namespace_id,
                RequestRecovery(
                    request_id=event.request_id,
                    execution_id=event.execution_id,
                    decision="awaiting_continuation",
                    outcome="paused",
                    unverified_tool_calls=unverified_tool_calls(log),
                    decided_at=self._clock(),
                ),
            )
            approval = self._permission_approvals.create(state, event, now=self._clock())
            if approval.route is None:
                approval = self._permission_approvals.reject_unrouted(approval.approval_id)
                self._schedule_permission_resume(approval)
            else:
                self._schedule_permission_notice(approval)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] permission approval could not be persisted: request=%s error_type=%s",
                event.request_id,
                type(exc).__name__,
            )

    def _schedule_permission_notice(self, approval: PermissionApproval) -> None:
        if approval.route is None or approval.route.channel == "web" or self._thinking_notice_sender is None:
            return
        route = approval.route
        calls = "\n".join(
            f"- {item.get('tool_call_id', 'call')}: {item.get('tool_name', 'tool')}: "
            f"{str(item.get('arguments') or '')[:500]}"
            for item in approval.intercepted
        )
        task = asyncio.create_task(
            self._send_permission_notice({
                "channel": route.channel,
                "app_id": route.app_id,
                "chat_id": route.target_id,
                "source_message_id": route.source_message_id,
                "idempotency_key": f"permission:{approval.approval_id}",
                "text": (
                    f"该员工需要确认一次工具操作（15 分钟内有效）：\n{calls}\n"
                    f"请在本会话逐个确认：批准 {approval.approval_id} <工具调用ID>；"
                    f"拒绝全部调用：拒绝 {approval.approval_id}。"
                ),
            }),
            name="jiuwenmate-permission-notice",
        )
        task.add_done_callback(_log_thinking_notice)

    async def _send_permission_notice(self, message: dict[str, object]) -> None:
        sender = self._thinking_notice_sender
        if sender is not None:
            await sender(message)

    def _schedule_permission_resume(self, approval: PermissionApproval) -> None:
        if approval.approval_id in self._permission_resume_tasks:
            return
        task = asyncio.create_task(self._resume_permission_approval(approval), name="jiuwenmate-permission-resume")
        self._permission_resume_tasks[approval.approval_id] = task

        def clear(_task):
            self._permission_resume_tasks.pop(approval.approval_id, None)

        task.add_done_callback(clear)

    async def _resume_permission_approval(self, approval: PermissionApproval) -> None:
        while True:
            state = await asyncio.to_thread(self._sessions.get_request, approval.namespace_id, approval.request_id)
            if state.status == "queued":
                await self._scheduler.wake(approval.namespace_id, approval.session_id)
                return
            if state.status == "ended":
                try:
                    self._scheduler.resume(state)
                except PersistentAgentError as exc:
                    if exc.code == "CONTINUATION_NOT_READY":
                        await asyncio.sleep(0.05)
                        continue
                    raise
                await self._scheduler.wake(approval.namespace_id, approval.session_id)
                return
            await asyncio.sleep(0.05)

    async def _permission_approval_loop(self) -> None:
        while True:
            expired = await asyncio.to_thread(self._permission_approvals.expire_pending, now=self._clock())
            for approval in expired:
                self._schedule_permission_resume(approval)
            await asyncio.sleep(2)

    async def _recover_permission_approvals(self) -> None:
        for approval in await asyncio.to_thread(self._permission_approvals.pending, "local"):
            if approval.route is None:
                approval = await asyncio.to_thread(self._permission_approvals.reject_unrouted, approval.approval_id)
                self._schedule_permission_resume(approval)
            else:
                self._schedule_permission_notice(approval)
        for approval in await asyncio.to_thread(self._permission_approvals.ready, "local"):
            self._schedule_permission_resume(approval)

    def _schedule_thinking_notice(self, event: core.AgentEvent) -> None:
        """Forward the waiting line participation already wrote. Ignore and a missing notice stay silent."""
        if self._thinking_notice_sender is None or event.event_type != core.EventType.PARTICIPATION_DECIDED:
            return
        if getattr(event.payload, "decision", None) not in ("respond", "intervene"):
            return
        notice = getattr(event.payload, "notice", None)
        if not isinstance(notice, str) or not notice.strip():
            return
        if event.execution_id in self._thinking_notices_sent:
            return
        self._thinking_notices_sent.add(event.execution_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._send_thinking_notice(event), name="jiuwenmate-thinking-notice")
        task.add_done_callback(_log_thinking_notice)

    async def _send_thinking_notice(self, event: core.AgentEvent) -> None:
        """Deliver the waiting line already chosen by participation. The host does not pick the words."""
        notice = getattr(event.payload, "notice", None)
        state = await asyncio.to_thread(self._sessions.get_request, event.namespace_id, event.request_id)
        route = state.record.route_snapshot
        sender = self._thinking_notice_sender
        if not isinstance(notice, str) or route is None or route.channel == "web" or sender is None:
            return
        await sender({
            "channel": route.channel,
            "app_id": route.app_id,
            "chat_id": route.target_id,
            "source_message_id": route.source_message_id,
            "text": notice,
            "idempotency_key": f"thinking:{event.execution_id}",
        })

    def _result(
        self, state: RequestState
    ) -> tuple[tuple[str, ...], tuple[dict[str, object], ...], str | None, ReplyDraft | None]:
        """Outcome of the latest Execution: handed-off replies, files and reason once it ended, else its draft."""
        record = state.record
        executions = self._sessions.executions(record.namespace_id, record.request_id)
        if not executions:
            return (), (), None, None
        latest = executions[-1]
        notices = tuple(
            row["notice_text"]
            for row in self._sessions.result_deliveries(record.namespace_id, record.request_id)
            if row["execution_id"] == latest.execution_id and row.get("notice_text")
        )
        log = self._sessions.read_execution_log(
            record.namespace_id, record.session_id, record.request_id, latest.execution_id
        )
        terminal = next((event for event in log if event.event_type in _TERMINAL_EVENTS), None)
        if terminal is None:
            return notices, (), None, None if state.status == "ended" else reply_draft(latest.execution_id, log)
        payload = terminal.payload
        replies: list[str] = []
        files: list[dict[str, object]] = []
        for output_id in payload.output_refs:
            output = self._sessions.candidate_output(record.namespace_id, output_id)
            if output is None:
                continue
            if output.payload.share_mode == "message" and output.payload.text:
                replies.append(output.payload.text)
            elif output.payload.share_mode in FILE_SHARE_MODES:
                files.append(self._file_view(record, output.payload))
        reason = payload.detail if terminal.event_type == core.EventType.EXECUTION_PAUSED else payload.reason
        if notices:
            reason = notices[-1]
        return (*replies, *notices), tuple(files), reason, None

    def _file_view(self, record, output: core.OutputPayload) -> dict[str, object]:
        """A handed-off file as a page may show it: name, size, caption and a signed download link."""
        from jiuwenswarm.common.file_download_token import build_file_download_info

        name = (output.file_path or "").rsplit("/", 1)[-1] or "file"
        path = self._files.path(record.namespace_id, record.agent_id, output)
        download_url = None
        if path is not None and path.is_file():
            download_url = build_file_download_info(str(path), name, record.session_id)["download_url"]
        size = output.file_size
        if path is not None and path.is_file():
            size = path.stat().st_size
        return {
            "output_id": output.output_id,
            "name": name,
            "path": output.file_path,
            "size": size,
            "caption": output.text,
            "share_mode": output.share_mode,
            "download_url": download_url,
        }

    @property
    def schedules(self) -> ScheduleService:
        """Plans for this runtime. Due ones are accepted on start and on each interval."""
        return self._schedules

    async def _schedule_loop(self) -> None:
        while True:
            try:
                await self._schedules.sweep()
            except Exception as exc:
                logger.warning("[JiuwenMate] schedule sweep failed: %s", exc)
            await asyncio.sleep(self._schedule_interval.total_seconds())

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
