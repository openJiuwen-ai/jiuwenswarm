"""Run one Execution through agent-core and report how it ended.

The driver builds this Execution's ports (the shared commit and budget ports, a
window backend bound to its own Session and the read scope of its colleague's
office directory), consumes core's event stream to the
end, and maps the terminal event to a Request outcome. When the stream ends,
core has already released its instance. Whatever happens, candidate copies are
forgotten and admitted model calls that were never reported are settled as
unknown usage. The driver never changes the Request's state itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Literal

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.conversation_search import ConversationSearch
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.group_search_cursor_repository import GroupSearchCursorRepository
from jiuwenswarm.server.jiuwenmate.execution_models import ExecutionRecord, RequestState
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.ports.budget import HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort, MembershipVerifier, unavailable_membership
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot, build_invocation
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from openjiuwen.harness import persistent_agent as core

logger = logging.getLogger(__name__)

AbortReason = Literal["cancel", "stop"]
ExecuteEntry = Callable[..., AsyncIterator[core.AgentEvent]]
AbortEntry = Callable[..., Awaitable[None]]
SnapshotSource = Callable[[str, str, str], ConfigSnapshot]

_TERMINAL = frozenset({core.EventType.EXECUTION_FINAL, core.EventType.EXECUTION_PAUSED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_execute(spec, context, inputs) -> AsyncIterator[core.AgentEvent]:
    return core.execute_persistent_request(spec, context, inputs)


async def _default_abort(context, *, reason: AbortReason) -> None:
    await core.abort_persistent_request(context, reason=reason)


def outcome_of(terminal: core.AgentEvent | None) -> str:
    """Request outcome for the Execution's terminal event; none at all means it failed."""
    if terminal is None:
        return "failed"
    if terminal.event_type == core.EventType.EXECUTION_FINAL:
        return terminal.payload.status
    return "stopped" if terminal.payload.reason == "stopped" else "paused"


class ExecutionHandle:
    """Control of one running Execution; a reason given before core starts is handed over on start."""

    def __init__(self, abort: AbortEntry) -> None:
        self._abort = abort
        self._context: core.PersistentBuildContext | None = None
        self.reason: AbortReason | None = None

    async def abort(self, reason: AbortReason) -> None:
        if self.reason is None:
            self.reason = reason
        if self._context is not None:
            await self._abort(self._context, reason=reason)

    async def _attach(self, context: core.PersistentBuildContext) -> None:
        self._context = context
        if self.reason is not None:
            await self._abort(context, reason=self.reason)


class ExecutionDriver:
    def __init__(
        self,
        *,
        snapshots: SnapshotSource,
        commit_port: HostCommitPort,
        queue: CommitQueue,
        budget_port: HostBudgetPort,
        budgets: BudgetRepository,
        windows: WindowBackendStore,
        workspaces: WorkspaceStore,
        groups: GroupChatRepository | None = None,
        group_archive: ConversationArchive | None = None,
        sessions: ExecutionRepository | None = None,
        verify_group_membership: MembershipVerifier = unavailable_membership,
        refresh_group_messages=None,
        execute: ExecuteEntry = _default_execute,
        abort: AbortEntry = _default_abort,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._snapshots = snapshots
        self._commit_port = commit_port
        self._queue = queue
        self._budget_port = budget_port
        self._budgets = budgets
        self._windows = windows
        self._workspaces = workspaces
        self._groups = groups
        self._group_archive = group_archive
        self._group_search = ConversationSearch(group_archive) if group_archive is not None else None
        self._group_search_cursors = (
            GroupSearchCursorRepository(groups.db_path) if groups is not None else None
        )
        self._sessions = sessions
        self._verify_group_membership = verify_group_membership
        self._refresh_group_messages = refresh_group_messages
        self._execute = execute
        self._abort = abort
        self._clock = clock

    def new_handle(self) -> ExecutionHandle:
        return ExecutionHandle(self._abort)

    async def run(self, state: RequestState, execution: ExecutionRecord, handle: ExecutionHandle) -> str:
        record = state.record
        try:
            snapshot = await asyncio.to_thread(
                self._snapshots, record.namespace_id, record.agent_id, record.config_version
            )
            workspace = HostWorkspaceAccess(self._workspaces, record.namespace_id, record.agent_id)
            group_chat_port = None
            if (record.request_input.kind in ("direct_message", "group_message") and self._groups is not None and
                    self._group_archive is not None and self._group_search is not None and
                    self._group_search_cursors is not None and self._sessions is not None):
                group_chat_port = HostGroupChatPort(
                    self._groups, self._group_archive, self._sessions,
                    namespace_id=record.namespace_id, agent_id=record.agent_id,
                    verify_membership=self._verify_group_membership,
                    refresh_messages=self._refresh_group_messages,
                    search=self._group_search,
                    cursors=self._group_search_cursors,
                    clock=self._clock,
                )
            ports = core.PersistentPorts(
                commit=self._commit_port,
                budget=self._budget_port,
                window_backend=self._windows.bind(record.namespace_id, record.agent_id, record.session_id),
                workspace_access=workspace,
                workspace_write_access=workspace,
                group_chat=group_chat_port,
            )
            spec, context = build_invocation(snapshot, record, execution, ports=ports)
            await handle._attach(context)
            inputs = record.request_input
            if record.request_input.kind == "group_message" and ports.group_chat is not None:
                trigger = await ports.group_chat.current_trigger(context.execution, inputs.trigger_message_id)
                if trigger.status in {"recalled", "unavailable"}:
                    raise PersistentAgentError("group trigger is no longer readable", code="TRIGGER_UNAVAILABLE")
                inputs = inputs.model_copy(update={"source": trigger,
                    "text": trigger.text or ("[附件消息]" if trigger.attachment_refs else "[群消息正文为空]"),
                    "attachment_refs": trigger.attachment_refs})
            terminal = None
            async with aclosing(self._execute(spec, context, inputs)) as events:
                async for event in events:
                    if event.event_type in _TERMINAL:
                        terminal = event
            if terminal is None:
                logger.warning(
                    "[JiuwenMate] execution ended without a terminal event: request=%s execution=%s",
                    record.request_id,
                    execution.execution_id,
                )
            return outcome_of(terminal)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] execution failed: request=%s execution=%s error=%s",
                record.request_id,
                execution.execution_id,
                exc,
            )
            return "failed"
        finally:
            self._queue.forget(record.namespace_id, execution.execution_id)
            try:
                await asyncio.to_thread(
                    self._budgets.settle_unreported, record.namespace_id, execution.execution_id, now=self._clock()
                )
            except Exception as exc:
                logger.warning(
                    "[JiuwenMate] unreported model calls not settled: execution=%s error=%s",
                    execution.execution_id,
                    exc,
                )
