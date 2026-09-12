# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-owned orchestration for cross-Session Agent messages."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jiuwenswarm.common.mode_matrix import deprecate_mode, is_single_agent_mode
from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageIdempotencyConflict,
    SessionMessageLimitExceeded,
    SessionMessageRecord,
    SessionMessageStore,
    SessionMessageStoreError,
)

logger = logging.getLogger(__name__)

MAX_SESSION_MESSAGE_BYTES = 32 * 1024
MAX_SESSION_MESSAGE_HOPS = 4
_STORE_RETRY_ATTEMPTS = 3
_STORE_RETRY_BASE_DELAY_SECONDS = 0.05
# Deadlock breaker, not a task deadline: a normal cross-session turn may run
# for a long time, but a wedged runtime stream must not hold the target's
# admission forever.
EXECUTION_WATCHDOG_TIMEOUT_SECONDS = 60 * 60


class SessionMessagingError(RuntimeError):
    """A stable tool-facing messaging failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionMessageSource:
    session_id: str
    request_id: str
    tool_call_id: str
    idempotency_key: str
    user_id: str = ""
    chain_id: str = ""
    parent_message_id: str = ""
    hop_count: int = 0


@dataclass(frozen=True, slots=True)
class SessionMessageExecutionResult:
    status: str
    error_code: str = ""
    error: str = ""


ExecuteSessionMessage = Callable[
    [SessionMessageRecord], Awaitable[SessionMessageExecutionResult]
]
StatusCallback = Callable[[SessionMessageRecord], Awaitable[None]]


class SessionMessageService:
    """Validate, persist and consume messages one at a time per target."""

    def __init__(
        self,
        *,
        store: SessionMessageStore,
        admission: Any,
        execute: ExecuteSessionMessage,
        status_callback: StatusCallback | None = None,
        available: bool = True,
        execution_watchdog_timeout: float = EXECUTION_WATCHDOG_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._admission = admission
        self._execute = execute
        self._status_callback = status_callback
        self._execution_watchdog_timeout = execution_watchdog_timeout
        self._available = asyncio.Event()
        if available:
            self._available.set()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._blocked_targets: set[str] = set()
        self._target_state_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._stopping = False

    @property
    def store(self) -> SessionMessageStore:
        return self._store

    @staticmethod
    def _is_transient_store_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).casefold()
        return "locked" in message or "busy" in message

    async def _store_call(self, operation: Callable[..., Any], /, *args, **kwargs):
        """Run a mailbox operation with bounded retries for SQLite contention."""

        for attempt in range(_STORE_RETRY_ATTEMPTS):
            try:
                return await asyncio.to_thread(operation, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if (
                    not self._is_transient_store_error(exc)
                    or attempt + 1 >= _STORE_RETRY_ATTEMPTS
                ):
                    raise
                delay = _STORE_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "[SessionMessaging] transient mailbox contention; retrying "
                    "operation=%s attempt=%d delay=%.3fs",
                    getattr(operation, "__name__", type(operation).__name__),
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await self._store_call(self._store.ensure_schema)
            recovered = await self._store_call(
                self._store.recover_inflight_as_unknown
            )
            if recovered:
                logger.warning(
                    "[SessionMessaging] quarantined %d in-flight messages after restart",
                    recovered,
                )
            self._started = True
            self._stopping = False
            targets = await self._store_call(self._store.queued_targets)
            for target_session_id in targets:
                self._ensure_worker(target_session_id)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            tasks = tuple(self._workers.values())
            self._workers.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lifecycle_lock:
            self._started = False

    async def set_available(self, available: bool) -> None:
        if available:
            self._available.set()
            if self._store.exists():
                await self.start()
                targets = await self._store_call(self._store.queued_targets)
                for target_session_id in targets:
                    self._ensure_worker(target_session_id)
        else:
            self._available.clear()

    @staticmethod
    def _metadata() -> tuple[list[dict[str, Any]], int]:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_all_sessions_metadata,
        )

        return get_all_sessions_metadata(limit=100_000, offset=0)

    @staticmethod
    def _session_metadata(session_id: str) -> dict[str, Any]:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        result = get_session_metadata(
            session_id,
            cache_bust=True,
            enable_writeback=False,
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _owner_matches(metadata: dict[str, Any], user_id: str) -> bool:
        stored_user = str(metadata.get("user_id") or "").strip()
        requested_user = str(user_id or "").strip()
        if requested_user:
            return stored_user == requested_user
        # Anonymous compatibility is limited to legacy local Sessions that are
        # themselves unowned.  It must never match a Session with an explicit
        # user because request.user_id is optional at the transport boundary.
        return not stored_user

    @staticmethod
    def _supported(metadata: dict[str, Any]) -> bool:
        mode = deprecate_mode(metadata.get("mode"))
        channel_id = str(metadata.get("channel_id") or "").strip().lower()
        return bool(
            is_single_agent_mode(mode)
            and channel_id in {"web", "tui"}
            and not str(metadata.get("cron_id") or "").strip()
        )

    @staticmethod
    def _utc_timestamp(value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return ""
        return (
            datetime.fromtimestamp(timestamp, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    async def list_targets(
        self,
        source: SessionMessageSource,
        *,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        await self.start()
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        normalized_query = str(query or "").strip().casefold()
        rows, _ = await asyncio.to_thread(self._metadata)
        eligible: list[dict[str, Any]] = []
        for metadata in rows:
            session_id = str(metadata.get("session_id") or "").strip()
            title = str(metadata.get("title") or "").strip()
            if not session_id or session_id == source.session_id:
                continue
            if not self._owner_matches(metadata, source.user_id):
                continue
            if not self._supported(metadata):
                continue
            if normalized_query and normalized_query not in title.casefold():
                continue
            eligible.append(metadata)

        eligible.sort(
            key=lambda value: float(value.get("last_message_at") or 0), reverse=True
        )
        page = eligible[offset:offset + limit]
        session_ids = [str(row["session_id"]) for row in page]
        pending_counts = await self._store_call(
            self._store.pending_counts, session_ids
        )
        blocking_states = await self._store_call(
            self._store.blocking_states, session_ids
        )
        sessions: list[dict[str, Any]] = []
        for metadata in page:
            session_id = str(metadata["session_id"])
            busy = bool(
                getattr(self._admission, "is_user_active", lambda _sid: False)(
                    session_id
                )
                or getattr(
                    self._admission,
                    "is_session_message_active",
                    lambda _sid: False,
                )(session_id)
                or getattr(
                    self._admission,
                    "is_heartbeat_active",
                    lambda _sid: False,
                )(session_id)
            )
            sessions.append(
                {
                    "session_id": session_id,
                    "title": str(metadata.get("title") or ""),
                    "mode": str(deprecate_mode(metadata.get("mode")) or "unknown"),
                    "project_id": str(metadata.get("project_id") or ""),
                    "runtime_state": blocking_states.get(
                        session_id, "busy" if busy else "idle"
                    ),
                    "pending_message_count": pending_counts.get(session_id, 0),
                    "last_message_at": self._utc_timestamp(
                        metadata.get("last_message_at")
                    ),
                }
            )
        return {
            "sessions": sessions,
            "total": len(eligible),
            "limit": limit,
            "offset": offset,
        }

    async def send_message(
        self,
        source: SessionMessageSource,
        *,
        target_session_id: str,
        message: str,
    ) -> dict[str, Any]:
        await self.start()
        target_session_id = str(target_session_id or "").strip()
        content = str(message or "").strip()
        if not is_valid_session_id(target_session_id):
            raise SessionMessagingError("INVALID_ARGUMENT", "invalid target_session_id")
        if target_session_id == source.session_id:
            raise SessionMessagingError(
                "SELF_SEND_NOT_ALLOWED", "cannot send a Session message to itself"
            )
        if not content:
            raise SessionMessagingError("INVALID_ARGUMENT", "message must not be empty")
        if len(content.encode("utf-8")) > MAX_SESSION_MESSAGE_BYTES:
            raise SessionMessagingError("INVALID_ARGUMENT", "message is too large")
        hop_count = source.hop_count + 1
        if hop_count > MAX_SESSION_MESSAGE_HOPS:
            raise SessionMessagingError(
                "LIMIT_EXCEEDED", "cross-Session message hop limit exceeded"
            )

        source_metadata, target_metadata = await asyncio.gather(
            asyncio.to_thread(self._session_metadata, source.session_id),
            asyncio.to_thread(self._session_metadata, target_session_id),
        )
        if not source_metadata or not target_metadata:
            raise SessionMessagingError(
                "NOT_FOUND_OR_FORBIDDEN", "target Session was not found"
            )
        if not self._owner_matches(
            source_metadata, source.user_id
        ) or not self._owner_matches(target_metadata, source.user_id):
            raise SessionMessagingError(
                "NOT_FOUND_OR_FORBIDDEN", "target Session was not found"
            )
        if not self._supported(source_metadata):
            raise SessionMessagingError(
                "UNSUPPORTED_SOURCE", "source Session does not support Agent messaging"
            )
        if not self._supported(target_metadata):
            raise SessionMessagingError(
                "UNSUPPORTED_TARGET", "target Session does not support Agent messaging"
            )

        owner_scope_id = str(source.user_id or "local").strip() or "local"
        async with self._target_state_lock:
            if target_session_id in self._blocked_targets:
                raise SessionMessagingError(
                    "NOT_FOUND_OR_FORBIDDEN", "target Session is being deleted"
                )
            current_target_metadata = await asyncio.to_thread(
                self._session_metadata, target_session_id
            )
            if (
                not current_target_metadata
                or not self._owner_matches(current_target_metadata, source.user_id)
                or not self._supported(current_target_metadata)
            ):
                raise SessionMessagingError(
                    "NOT_FOUND_OR_FORBIDDEN", "target Session was not found"
                )
            try:
                record, created = await self._store_call(
                    self._store.enqueue,
                    owner_scope_id=owner_scope_id,
                    source_session_id=source.session_id,
                    source_title_snapshot=str(source_metadata.get("title") or ""),
                    source_request_id=source.request_id,
                    source_tool_call_id=source.tool_call_id,
                    idempotency_key=source.idempotency_key,
                    target_session_id=target_session_id,
                    content=content,
                    chain_id=source.chain_id,
                    parent_message_id=source.parent_message_id,
                    hop_count=hop_count,
                )
            except SessionMessageLimitExceeded as exc:
                raise SessionMessagingError("LIMIT_EXCEEDED", str(exc)) from exc
            except SessionMessageIdempotencyConflict as exc:
                raise SessionMessagingError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
            except (OSError, sqlite3.Error, SessionMessageStoreError) as exc:
                raise SessionMessagingError(
                    "PERSISTENCE_UNAVAILABLE", str(exc)
                ) from exc

        self._ensure_worker(target_session_id)
        if created:
            await self._notify(record)
        return {
            "message_id": record.message_id,
            "target_session_id": record.target_session_id,
            "accepted": True,
            "status": record.status,
            "deduplicated": not created,
        }

    async def list_messages(
        self,
        source: SessionMessageSource,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List mailbox records visible to the calling Session."""

        await self.start()
        metadata = await asyncio.to_thread(self._session_metadata, source.session_id)
        if (
            not metadata
            or not self._owner_matches(metadata, source.user_id)
            or not self._supported(metadata)
        ):
            raise SessionMessagingError(
                "NOT_FOUND_OR_FORBIDDEN", "source Session was not found"
            )
        owner_scope_id = str(source.user_id or "local").strip() or "local"
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        records = await self._store_call(
            self._store.list_messages,
            owner_scope_id=owner_scope_id,
            session_id=source.session_id,
            limit=limit,
            offset=offset,
        )
        return {
            "messages": [self._message_projection(record) for record in records],
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _message_projection(record: SessionMessageRecord) -> dict[str, Any]:
        """Expose mailbox state without leaking host-only routing identifiers."""

        return {
            "message_id": record.message_id,
            "source_session_id": record.source_session_id,
            "source_title": record.source_title_snapshot,
            "target_session_id": record.target_session_id,
            "content": record.content,
            "chain_id": record.chain_id,
            "parent_message_id": record.parent_message_id,
            "hop_count": record.hop_count,
            "status": record.status,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "updated_at": record.updated_at,
            "last_error_code": record.last_error_code,
            "last_error": record.last_error,
            "resolved_at": record.resolved_at,
            "resolution": record.resolution,
            "retry_of": record.retry_of,
        }

    async def resolve_unknown(
        self,
        source: SessionMessageSource,
        *,
        message_id: str,
        resolution: str,
    ) -> dict[str, Any]:
        """Resolve one uncertain outcome explicitly, never by automatic replay."""

        await self.start()
        metadata = await asyncio.to_thread(self._session_metadata, source.session_id)
        if (
            not metadata
            or not self._owner_matches(metadata, source.user_id)
            or not self._supported(metadata)
        ):
            raise SessionMessagingError(
                "NOT_FOUND_OR_FORBIDDEN", "source Session was not found"
            )
        normalized_resolution = str(resolution or "").strip().lower()
        if normalized_resolution not in {"succeeded", "cancelled"}:
            raise SessionMessagingError(
                "INVALID_ARGUMENT", "resolution must be succeeded or cancelled"
            )
        owner_scope_id = str(source.user_id or "local").strip() or "local"
        record = await self._store_call(
            self._store.resolve_unknown,
            str(message_id or "").strip(),
            owner_scope_id=owner_scope_id,
            participant_session_id=source.session_id,
            resolution=normalized_resolution,
        )
        if record is None:
            raise SessionMessagingError(
                "NOT_FOUND_OR_INVALID_STATE",
                "unknown Session message was not found or is already resolved",
            )
        await self._notify(record)
        self._ensure_worker(record.target_session_id)
        return {
            "message_id": record.message_id,
            "target_session_id": record.target_session_id,
            "status": record.status,
            "resolution": record.resolution,
        }

    async def mark_waiting(
        self,
        message_id: str,
        *,
        interrupt_request_id: str,
        interrupt_source: str,
    ) -> bool:
        """Persist a HITL correlation before its question is pushed."""

        record = await self._store_call(
            self._store.mark_waiting,
            message_id,
            interrupt_request_id=interrupt_request_id,
            interrupt_source=interrupt_source,
        )
        if record is not None:
            await self._notify(record)
        return record is not None

    async def complete_waiting_after_resume(
        self,
        target_session_id: str,
        *,
        interrupt_request_id: str,
        interrupt_source: str,
        waiting_user: bool,
        failed: bool,
        error: str = "",
        next_interrupt_request_id: str = "",
        next_interrupt_source: str = "",
    ) -> bool:
        """Advance the mailbox item resumed by a target user's interrupt answer."""

        record = await self.find_waiting_for_resume(
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )
        if record is None:
            return False
        if waiting_user:
            next_request_id = str(next_interrupt_request_id or "").strip()
            next_source = str(next_interrupt_source or "").strip()
            if not next_request_id or not next_source:
                return False
            updated = await self._store_call(
                self._store.replace_waiting_correlation,
                record.message_id,
                expected_interrupt_request_id=interrupt_request_id,
                expected_interrupt_source=interrupt_source,
                interrupt_request_id=next_request_id,
                interrupt_source=next_source,
            )
        else:
            return await self.finalize_waiting_message(
                message_id=record.message_id,
                target_session_id=target_session_id,
                status="failed" if failed else "succeeded",
                error_code="EXECUTION_FAILED" if failed else "",
                error=error,
            )
        if updated is not None:
            await self._notify(updated)
        return updated is not None

    async def find_waiting_for_resume(
        self,
        target_session_id: str,
        interrupt_request_id: str,
        interrupt_source: str,
    ) -> SessionMessageRecord | None:
        """Return the exact mailbox item addressed by an interrupt answer."""

        await self.start()
        return await self._store_call(
            self._store.waiting_for_resume,
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )

    async def finalize_waiting_message(
        self,
        *,
        message_id: str,
        target_session_id: str,
        status: str,
        error_code: str = "",
        error: str = "",
    ) -> bool:
        """Move a resumed mailbox item to a durable terminal state."""

        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError(f"unsupported resumed-message status: {status}")
        updated = await self._store_call(
            self._store.transition_status,
            message_id,
            status,
            expected_statuses=("waiting_user",),
            error_code=error_code,
            error=error,
        )
        if updated is not None:
            await self._notify(updated)
            self._ensure_worker(target_session_id)
        return updated is not None

    async def supersede_waiting_for_target(self, target_session_id: str) -> int:
        """Fail waiting items a plain user turn has bypassed, then re-arm FIFO."""

        target_session_id = str(target_session_id or "").strip()
        if not target_session_id:
            return 0
        await self.start()
        if not self._store.exists():
            return 0
        records = await self._store_call(
            self._store.supersede_waiting_for_target, target_session_id
        )
        for record in records:
            await self._notify(record)
        if records:
            self._ensure_worker(target_session_id)
        return len(records)

    async def on_target_deleted(self, target_session_id: str) -> int:
        """Cancel durable work that can no longer run for a deleted Session."""

        await self.start()
        await self.begin_target_delete(target_session_id)
        records = await self._store_call(
            self._store.cancel_pending_for_target, target_session_id
        )
        for record in records:
            await self._notify(record)
        async with self._target_state_lock:
            self._blocked_targets.discard(target_session_id)
        return len(records)

    async def begin_target_delete(self, target_session_id: str) -> None:
        """Stop a target consumer before its Session directory is removed."""

        target_session_id = str(target_session_id or "").strip()
        if not target_session_id:
            return
        async with self._target_state_lock:
            self._blocked_targets.add(target_session_id)
        task = self._workers.pop(target_session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def abort_target_delete(self, target_session_id: str) -> None:
        """Resume queued work when the surrounding Session deletion fails."""

        target_session_id = str(target_session_id or "").strip()
        if not target_session_id:
            return
        async with self._target_state_lock:
            self._blocked_targets.discard(target_session_id)
        if self._store.exists():
            record = await self._store_call(
                self._store.next_queued, target_session_id
            )
            if record is not None:
                self._ensure_worker(target_session_id)

    def _ensure_worker(self, target_session_id: str) -> None:
        task = self._workers.get(target_session_id)
        worker_busy = task is not None and not task.done()
        target_blocked = target_session_id in self._blocked_targets
        if self._stopping or target_blocked or worker_busy:
            return
        worker = asyncio.create_task(
            self._consume_target(target_session_id),
            name=f"session-message:{target_session_id}",
        )
        self._workers[target_session_id] = worker
        worker.add_done_callback(
            lambda completed, sid=target_session_id: self._worker_finished(
                sid, completed
            )
        )

    def _worker_finished(
        self, target_session_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._workers.get(target_session_id) is task:
            self._workers.pop(target_session_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception(
                "[SessionMessaging] target worker failed: session_id=%s",
                target_session_id,
            )
        if not self._stopping:
            asyncio.create_task(self._restart_worker_if_queued(target_session_id))

    async def _restart_worker_if_queued(self, target_session_id: str) -> None:
        """Close the enqueue/worker-exit race without polling idle targets."""

        try:
            record = await self._store_call(
                self._store.next_queued, target_session_id
            )
        except Exception:
            logger.exception(
                "[SessionMessaging] failed to recheck target queue: session_id=%s",
                target_session_id,
            )
            return
        if record is not None and not self._stopping:
            self._ensure_worker(target_session_id)

    async def _notify(self, record: SessionMessageRecord) -> None:
        if self._status_callback is None:
            return
        try:
            await self._status_callback(record)
        except Exception:
            logger.debug("[SessionMessaging] status push failed", exc_info=True)

    async def _consume_target(self, target_session_id: str) -> None:
        while not self._stopping:
            if target_session_id in self._blocked_targets:
                return
            await self._available.wait()
            if target_session_id in self._blocked_targets:
                return
            try:
                record = await self._store_call(
                    self._store.next_queued, target_session_id
                )
            except sqlite3.OperationalError as exc:
                if not self._is_transient_store_error(exc):
                    raise
                logger.warning(
                    "[SessionMessaging] mailbox remains busy; keeping target "
                    "worker alive: session_id=%s",
                    target_session_id,
                    exc_info=True,
                )
                await asyncio.sleep(_STORE_RETRY_BASE_DELAY_SECONDS)
                continue
            if record is None:
                return
            run_id = f"smrun_{uuid.uuid4().hex}"
            acquired = False
            claimed: SessionMessageRecord | None = None
            try:
                await self._admission.begin_session_message(target_session_id, run_id)
                acquired = True
                if (
                    not self._available.is_set()
                    or self._stopping
                    or target_session_id in self._blocked_targets
                ):
                    continue
                execution_request_id = f"session-message-{record.message_id}"
                claimed = await self._store_call(
                    self._store.claim,
                    record.message_id,
                    execution_request_id,
                    run_id,
                )
                if claimed is None:
                    continue
                await self._notify(claimed)
                try:
                    result = await asyncio.wait_for(
                        self._execute(claimed),
                        timeout=self._execution_watchdog_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "[SessionMessaging] execution watchdog fired; outcome "
                        "is uncertain: message_id=%s",
                        claimed.message_id,
                    )
                    result = SessionMessageExecutionResult(
                        status="unknown",
                        error_code="EXECUTION_WATCHDOG_TIMEOUT",
                        error=(
                            "execution did not finish within "
                            f"{self._execution_watchdog_timeout}s and was "
                            "cancelled; its outcome is unknown"
                        ),
                    )
                if result.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "waiting_user",
                    "unknown",
                }:
                    raise ValueError(
                        f"unsupported execution result status: {result.status}"
                    )
                if result.status == "waiting_user":
                    current = await self._store_call(
                        self._store.get, claimed.message_id
                    )
                    updated = (
                        current
                        if current is not None and current.status == "waiting_user"
                        else await self._store_call(
                            self._store.transition_status,
                            claimed.message_id,
                            result.status,
                            expected_statuses=("running",),
                            error_code=result.error_code,
                            error=result.error,
                        )
                    )
                else:
                    updated = await self._store_call(
                        self._store.transition_status,
                        claimed.message_id,
                        result.status,
                        expected_statuses=("running", "waiting_user"),
                        error_code=result.error_code,
                        error=result.error,
                    )
                if updated is not None:
                    await self._notify(updated)
            except asyncio.CancelledError:
                if claimed is not None:
                    updated = await self._store_call(
                        self._store.transition_status,
                        claimed.message_id,
                        "unknown",
                        expected_statuses=("running", "waiting_user"),
                        error_code="HOST_INTERRUPTED",
                        error="execution was interrupted before its outcome was confirmed",
                    )
                    if updated is not None:
                        await self._notify(updated)
                raise
            except sqlite3.OperationalError as exc:
                if not self._is_transient_store_error(exc):
                    raise
                if claimed is None:
                    logger.warning(
                        "[SessionMessaging] mailbox contention before claim; "
                        "leaving message queued: message_id=%s",
                        record.message_id,
                        exc_info=True,
                    )
                    await asyncio.sleep(_STORE_RETRY_BASE_DELAY_SECONDS)
                    continue
                logger.exception(
                    "[SessionMessaging] mailbox contention after claim: message_id=%s",
                    claimed.message_id,
                )
                updated = await self._store_call(
                    self._store.transition_status,
                    claimed.message_id,
                    "unknown",
                    expected_statuses=("running", "waiting_user"),
                    error_code="EXECUTION_OUTCOME_UNKNOWN",
                    error=str(exc),
                )
                if updated is not None:
                    await self._notify(updated)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "[SessionMessaging] message execution failed: message_id=%s",
                    record.message_id,
                )
                if claimed is None:
                    updated = await self._store_call(
                        self._store.transition_status,
                        record.message_id,
                        "failed",
                        expected_statuses=("queued",),
                        error_code="ADMISSION_FAILED",
                        error=str(exc),
                    )
                else:
                    updated = await self._store_call(
                        self._store.transition_status,
                        claimed.message_id,
                        "unknown",
                        expected_statuses=("running", "waiting_user"),
                        error_code=getattr(
                            exc, "code", "EXECUTION_OUTCOME_UNKNOWN"
                        ),
                        error=str(exc),
                    )
                if updated is not None:
                    await self._notify(updated)
            finally:
                if acquired:
                    await self._admission.end_session_message(target_session_id, run_id)


__all__ = [
    "MAX_SESSION_MESSAGE_BYTES",
    "MAX_SESSION_MESSAGE_HOPS",
    "SessionMessageExecutionResult",
    "SessionMessageService",
    "SessionMessageSource",
    "SessionMessagingError",
]
