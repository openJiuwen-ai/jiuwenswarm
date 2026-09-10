"""Product-level Session lifecycle and execution coordinator."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from jiuwenswarm.runtime.session.execution_registry import SessionExecutionRegistry
from jiuwenswarm.runtime.session.model import (
    CancelExecutionResult,
    CloseSessionResult,
    RuntimeSessionSnapshot,
    RuntimeSessionState,
    SessionExecutionHandle,
    SessionExecutionSnapshot,
    SessionExecutionState,
    SessionPersistencePolicy,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session.work_scheduler import SessionWorkScheduler

T = TypeVar("T")


@dataclass(slots=True)
class _SessionRecord:
    session_id: str
    channel_id: str
    persistence_policy: SessionPersistencePolicy
    generation: int
    state: RuntimeSessionState = RuntimeSessionState.READY


@dataclass(slots=True)
class _StreamItem:
    value: Any = None
    error: BaseException | None = None
    done: bool = False


class RuntimeSessionCoordinator:
    """Owns ephemeral execution state, not durable Session metadata/history."""

    def __init__(
        self,
        *,
        registry: SessionExecutionRegistry | None = None,
        scheduler: SessionWorkScheduler | None = None,
        cancel_timeout: float = 5.0,
        stream_buffer_size: int = 64,
    ) -> None:
        self._registry = registry or SessionExecutionRegistry()
        self._scheduler = scheduler or SessionWorkScheduler()
        self._cancel_timeout = cancel_timeout
        self._stream_buffer_size = max(1, stream_buffer_size)
        self._sessions: dict[str, _SessionRecord] = {}
        self._generations: dict[str, int] = {}
        self._control_claims: set[tuple[str, int, str, str]] = set()
        self._accepting = True
        self._lock = asyncio.Lock()

    async def register_session(
        self,
        session_id: str,
        channel_id: str,
        persistence_policy: SessionPersistencePolicy = SessionPersistencePolicy.PERSISTENT,
    ) -> RuntimeSessionSnapshot:
        normalized = str(session_id).strip()
        if not normalized:
            raise ValueError("session_id is required")
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("session coordinator is closed")
            current = self._sessions.get(normalized)
            if current is None or current.state is RuntimeSessionState.CLOSED:
                generation = self._generations.get(normalized, 0) + 1
                self._generations[normalized] = generation
                current = _SessionRecord(
                    session_id=normalized,
                    channel_id=channel_id or "default",
                    persistence_policy=persistence_policy,
                    generation=generation,
                )
                self._sessions[normalized] = current
            else:
                current.channel_id = channel_id or current.channel_id
                current.persistence_policy = persistence_policy
            return self._snapshot(current)

    async def run_unary(
        self,
        session_id: str,
        request_id: str,
        work_kind: SessionWorkKind,
        operation: Callable[[], Awaitable[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
        wait_for_terminal: bool = False,
    ) -> T:
        record = self._require_open_session(session_id)
        handle = self._new_execution(record, request_id, work_kind)
        handle.retain_task_while_waiting = wait_for_terminal

        async def tracked() -> T:
            self._registry.mark_running(handle)
            record.state = RuntimeSessionState.ACTIVE
            try:
                value = await operation()
            except asyncio.CancelledError as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED, error=exc
                )
                raise
            except BaseException as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                raise
            else:
                control_id = (
                    suspension_key(value) if suspension_key is not None else None
                )
                if control_id:
                    self._registry.mark_awaiting_control(handle, control_id)
                if handle.waiting_control_id:
                    self._registry.mark_waiting(handle)
                else:
                    self._registry.mark_terminal(
                        handle, SessionExecutionState.SUCCEEDED
                    )
                return value
            finally:
                self._refresh_session_state(record)

        try:
            if work_kind.scheduled:
                return await self._scheduler.submit_and_wait(handle, tracked)
            handle.task = asyncio.current_task()
            value = await tracked()
            if wait_for_terminal and not handle.state.terminal:
                handle.task = asyncio.current_task()
                await handle.terminal_event.wait()
                if handle.state is SessionExecutionState.CANCELLED:
                    raise asyncio.CancelledError(handle.error or "execution cancelled")
                if handle.state is SessionExecutionState.FAILED:
                    raise RuntimeError(handle.error or "execution failed")
            return value
        except asyncio.CancelledError:
            handle.cancellation_requested = True
            if work_kind.scheduled:
                await self._scheduler.cancel_handles(
                    [handle], wait_timeout=self._cancel_timeout
                )
            if not handle.state.terminal:
                self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED)
            await self._cancel_descendants(handle)
            self._refresh_session_state(record)
            raise
        except BaseException:
            await self._cancel_descendants(handle)
            raise
        finally:
            if not work_kind.scheduled and handle.task is asyncio.current_task():
                handle.task = None

    def record_interaction(
        self, session_id: str, request_id: str, control_id: str
    ) -> bool:
        """Associate an interaction emitted inside an owned callback execution."""
        record = self._sessions.get(session_id)
        if record is None or record.state is not RuntimeSessionState.ACTIVE:
            return False
        for handle in self._registry.select(
            session_id=session_id,
            request_id=request_id,
            generation=record.generation,
            active_only=True,
        ):
            if handle.state is SessionExecutionState.RUNNING:
                self._registry.mark_awaiting_control(handle, control_id)
                return True
        return False

    async def deliver_control(
        self,
        session_id: str,
        request_id: str,
        operation: Callable[[], Awaitable[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
    ) -> T:
        """Deliver input to the running Session work without joining its lane."""
        record = self._require_open_session(session_id)
        if self._is_control_claimed(record, request_id):
            raise RuntimeError("control input is already being delivered")
        parent = self._control_parent(record, request_id)
        if parent is None:
            raise RuntimeError(f"session has no active execution: {session_id}")
        claim = (session_id, record.generation, parent.execution_id, request_id)
        self._control_claims.add(claim)
        parent_was_waiting = (
            parent.state is SessionExecutionState.WAITING_FOR_CONTROL
        )
        if parent_was_waiting:
            self._registry.resume_waiting(parent)
        handle = self._new_execution(
            record,
            request_id,
            SessionWorkKind.CONTROL_INPUT,
            parent_execution_id=parent.execution_id,
        )
        handle.task = asyncio.current_task()
        self._registry.mark_running(handle)
        try:
            try:
                value = await operation()
            except asyncio.CancelledError as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED, error=exc
                )
                if parent_was_waiting and not parent.state.terminal:
                    self._registry.mark_awaiting_control(parent, request_id)
                    self._registry.mark_waiting(parent)
                raise
            except BaseException as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                if parent_was_waiting and not parent.state.terminal:
                    self._registry.mark_awaiting_control(parent, request_id)
                    self._registry.mark_waiting(parent)
                raise

            if (
                parent.state.terminal
                or self._sessions.get(session_id) is not record
                or record.state in {
                    RuntimeSessionState.QUIESCING,
                    RuntimeSessionState.CLOSED,
                }
            ):
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED
                )
                raise asyncio.CancelledError("parent execution ended")

            control_id = suspension_key(value) if suspension_key is not None else None
            heartbeat_root = self._heartbeat_root(parent)
            if control_id and heartbeat_root is not None:
                self._registry.mark_awaiting_control(heartbeat_root, control_id)
                self._registry.mark_waiting(heartbeat_root)
                self._registry.mark_terminal(handle, SessionExecutionState.SUCCEEDED)
            else:
                parent_finished_while_delivering = (
                    parent.state is SessionExecutionState.WAITING_FOR_CONTROL
                    and parent.waiting_control_id == request_id
                )
                if parent_was_waiting or parent_finished_while_delivering:
                    self._registry.mark_terminal(
                        parent, SessionExecutionState.SUCCEEDED
                    )
                elif parent.waiting_control_id == request_id:
                    parent.waiting_control_id = None
            if control_id and heartbeat_root is None:
                self._registry.mark_awaiting_control(handle, control_id)
                self._registry.mark_waiting(handle)
            elif not control_id:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.SUCCEEDED
                )
            return value
        finally:
            self._control_claims.discard(claim)
            self._refresh_session_state(record)

    def has_control_target(self, session_id: str, request_id: str) -> bool:
        """Return whether control input can resume a live Session execution."""
        record = self._sessions.get(session_id)
        if record is None or record.state is RuntimeSessionState.CLOSED:
            return False
        return self._is_control_claimed(
            record, request_id
        ) or self._control_parent(record, request_id) is not None

    def control_target_work_kind(
        self, session_id: str, request_id: str
    ) -> SessionWorkKind | None:
        record = self._sessions.get(session_id)
        if record is None or record.state is RuntimeSessionState.CLOSED:
            return None
        parent = self._control_parent(record, request_id)
        if parent is None:
            parent = self._claimed_control_parent(record, request_id)
        root = None if parent is None else self._heartbeat_root(parent)
        return root.work_kind if root is not None else None

    async def run_stream(
        self,
        session_id: str,
        request_id: str,
        work_kind: SessionWorkKind,
        operation: Callable[[], AsyncIterator[T] | Awaitable[AsyncIterator[T]]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
    ) -> AsyncIterator[T]:
        record = self._require_open_session(session_id)
        handle = self._new_execution(record, request_id, work_kind)
        queue: asyncio.Queue[_StreamItem] = asyncio.Queue(self._stream_buffer_size)

        async def produce() -> None:
            self._registry.mark_running(handle)
            record.state = RuntimeSessionState.ACTIVE
            stream: AsyncIterator[T] | None = None
            terminal_item: _StreamItem | None = None
            try:
                candidate = operation()
                stream = await candidate if inspect.isawaitable(candidate) else candidate
                async for item in stream:
                    control_id = (
                        suspension_key(item) if suspension_key is not None else None
                    )
                    if control_id:
                        self._registry.mark_awaiting_control(handle, control_id)
                    await queue.put(_StreamItem(value=item))
            except asyncio.CancelledError as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED, error=exc
                )
                terminal_item = _StreamItem(error=exc, done=True)
                raise
            except BaseException as exc:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                terminal_item = _StreamItem(error=exc, done=True)
            else:
                if handle.waiting_control_id:
                    self._registry.mark_waiting(handle)
                else:
                    self._registry.mark_terminal(
                        handle, SessionExecutionState.SUCCEEDED
                    )
                terminal_item = _StreamItem(done=True)
            finally:
                if stream is not None:
                    close = getattr(stream, "aclose", None)
                    if callable(close):
                        await close()
                if handle.task is asyncio.current_task():
                    handle.task = None
                self._refresh_session_state(record)
                # A terminal marker must not be dropped when
                # the bounded buffer is full, otherwise the consumer can wait
                # forever after draining the final data item.
                if terminal_item is not None:
                    await queue.put(terminal_item)

        scheduled = asyncio.create_task(
            self._scheduler.submit_and_wait(handle, produce)
            if work_kind.scheduled
            else produce()
        )
        if not work_kind.scheduled:
            handle.task = scheduled
        completed_normally = False
        try:
            while True:
                item = await queue.get()
                if item.error is not None:
                    raise item.error
                if item.done:
                    completed_normally = True
                    break
                yield item.value
            await scheduled
        finally:
            if not completed_normally and not scheduled.done():
                await self.cancel_execution(
                    session_id,
                    execution_id=handle.execution_id,
                )
            if scheduled.done():
                try:
                    scheduled.result()
                except (asyncio.CancelledError, Exception):
                    pass

    async def cancel_execution(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        execution_id: str | None = None,
        generation: int | None = None,
        wait_timeout: float | None = None,
    ) -> CancelExecutionResult:
        handles = self._registry.select(
            session_id=session_id,
            request_id=request_id,
            execution_id=execution_id,
            generation=generation,
            active_only=True,
        )
        handles = self._with_descendants(handles)
        for handle in handles:
            handle.cancellation_requested = True
        direct = [handle for handle in handles if not handle.work_kind.scheduled]
        work = [handle for handle in handles if handle.work_kind.scheduled]
        timed_out = await self._scheduler.cancel_handles(
            work,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        direct_timeouts = await self._cancel_direct_handles(
            direct,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        timed_out = (*timed_out, *direct_timeouts)
        timed_out_set = set(timed_out)
        cancelled = 0
        for handle in handles:
            if handle.execution_id not in timed_out_set and not handle.state.terminal:
                self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED)
            if handle.state is SessionExecutionState.CANCELLED:
                cancelled += 1
        return CancelExecutionResult(
            matched=len(handles), cancelled=cancelled, timed_out=timed_out
        )

    async def close_session(
        self,
        session_id: str,
        *,
        generation: int | None = None,
        wait_timeout: float | None = None,
    ) -> CloseSessionResult:
        record = self._sessions.get(session_id)
        if record is None or (generation is not None and record.generation != generation):
            return CloseSessionResult(session_id, generation, False)
        target_generation = record.generation
        record.state = RuntimeSessionState.QUIESCING
        active = self._registry.select(
            session_id=session_id,
            generation=target_generation,
            active_only=True,
        )
        direct = [handle for handle in active if not handle.work_kind.scheduled]
        direct_timeouts = await self._cancel_direct_handles(
            direct,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        _existed, timed_out = await self._scheduler.close_session(
            session_id,
            generation=target_generation,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        timed_out = (*timed_out, *direct_timeouts)
        for handle in self._registry.select(
            session_id=session_id, generation=target_generation, active_only=True
        ):
            handle.cancellation_requested = True
            if handle.execution_id not in timed_out and not handle.state.terminal:
                self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED)
        if self._sessions.get(session_id) is record:
            record.state = RuntimeSessionState.CLOSED
        return CloseSessionResult(session_id, target_generation, True, timed_out)

    def get_execution(self, execution_id: str) -> SessionExecutionSnapshot | None:
        handle = self._registry.get(execution_id)
        return None if handle is None else handle.snapshot()

    def snapshot_session(self, session_id: str) -> RuntimeSessionSnapshot | None:
        record = self._sessions.get(session_id)
        return None if record is None else self._snapshot(record)

    async def close(self) -> None:
        async with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            records = tuple(self._sessions.values())
        errors: list[BaseException] = []
        for record in records:
            if record.state is RuntimeSessionState.CLOSED:
                continue
            try:
                await self.close_session(
                    record.session_id,
                    generation=record.generation,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            await self._scheduler.close(wait_timeout=self._cancel_timeout)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise errors[0]

    def _require_open_session(self, session_id: str) -> _SessionRecord:
        if not self._accepting:
            raise RuntimeError("session coordinator is closed")
        record = self._sessions.get(session_id)
        if record is None:
            raise RuntimeError(f"session is not registered: {session_id}")
        if record.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}:
            raise RuntimeError(f"session is not accepting work: {session_id}")
        return record

    def _new_execution(
        self,
        record: _SessionRecord,
        request_id: str,
        work_kind: SessionWorkKind,
        *,
        parent_execution_id: str | None = None,
    ) -> SessionExecutionHandle:
        superseded_kinds: set[SessionWorkKind] = set()
        if work_kind in {SessionWorkKind.CHAT_UNARY, SessionWorkKind.CHAT_STREAM}:
            superseded_kinds = {
                SessionWorkKind.CHAT_UNARY,
                SessionWorkKind.CHAT_STREAM,
                SessionWorkKind.HEARTBEAT,
            }
        elif work_kind is SessionWorkKind.GOAL_STREAM:
            superseded_kinds = {
                SessionWorkKind.GOAL_STREAM,
                SessionWorkKind.GOAL_ATTACH,
            }
        if superseded_kinds:
            for previous in self._registry.select(
                session_id=record.session_id,
                generation=record.generation,
                active_only=True,
            ):
                if (
                    previous.work_kind in superseded_kinds
                    and previous.state is SessionExecutionState.WAITING_FOR_CONTROL
                ):
                    previous.cancellation_requested = True
                    self._registry.mark_terminal(
                        previous, SessionExecutionState.CANCELLED
                    )
        handle = SessionExecutionHandle(
            execution_id=uuid.uuid4().hex,
            session_id=record.session_id,
            request_id=str(request_id or ""),
            generation=record.generation,
            work_kind=work_kind,
            parent_execution_id=parent_execution_id,
        )
        self._registry.register(handle)
        record.state = RuntimeSessionState.ACTIVE
        return handle

    @staticmethod
    async def _cancel_direct_handles(
        handles: list[SessionExecutionHandle],
        *,
        wait_timeout: float | None,
    ) -> tuple[str, ...]:
        tasks: dict[str, asyncio.Task[Any]] = {}
        current = asyncio.current_task()
        for handle in handles:
            task = handle.task
            if task is None or task.done() or task is current:
                continue
            task.cancel()
            tasks[handle.execution_id] = task
        if not tasks:
            return ()
        _done, pending = await asyncio.wait(set(tasks.values()), timeout=wait_timeout)
        return tuple(
            execution_id
            for execution_id, task in tasks.items()
            if task in pending
        )

    def _control_parent(
        self, record: _SessionRecord, request_id: str
    ) -> SessionExecutionHandle | None:
        parents = [
            handle
            for handle in self._registry.select(
                session_id=record.session_id,
                generation=record.generation,
                active_only=True,
            )
            if handle.waiting_control_id == request_id
            and handle.state
            in {
                SessionExecutionState.RUNNING,
                SessionExecutionState.WAITING_FOR_CONTROL,
            }
        ]
        if not parents:
            return None
        return max(parents, key=lambda handle: handle.started_at or handle.created_at)

    def _is_control_claimed(
        self, record: _SessionRecord, request_id: str
    ) -> bool:
        return any(
            claimed_session == record.session_id
            and claimed_generation == record.generation
            and claimed_request == request_id
            for (
                claimed_session,
                claimed_generation,
                _claimed_execution,
                claimed_request,
            ) in self._control_claims
        )

    def _claimed_control_parent(
        self, record: _SessionRecord, request_id: str
    ) -> SessionExecutionHandle | None:
        for (
            claimed_session,
            claimed_generation,
            claimed_execution,
            claimed_request,
        ) in self._control_claims:
            if (
                claimed_session == record.session_id
                and claimed_generation == record.generation
                and claimed_request == request_id
            ):
                return self._registry.get(claimed_execution)
        return None

    def _heartbeat_root(
        self, handle: SessionExecutionHandle
    ) -> SessionExecutionHandle | None:
        current = handle
        seen: set[str] = set()
        while current.execution_id not in seen:
            seen.add(current.execution_id)
            if current.work_kind is SessionWorkKind.HEARTBEAT:
                return current
            if current.parent_execution_id is None:
                return None
            parent = self._registry.get(current.parent_execution_id)
            if (
                parent is None
                or parent.session_id != handle.session_id
                or parent.generation != handle.generation
            ):
                return None
            current = parent
        return None

    def _with_descendants(
        self, handles: list[SessionExecutionHandle]
    ) -> list[SessionExecutionHandle]:
        selected = {handle.execution_id: handle for handle in handles}
        frontier = set(selected)
        while frontier:
            child_ids: set[str] = set()
            scopes = {
                (handle.session_id, handle.generation)
                for handle in selected.values()
            }
            for session_id, generation in scopes:
                for handle in self._registry.select(
                    session_id=session_id,
                    generation=generation,
                    active_only=True,
                ):
                    if (
                        handle.parent_execution_id in frontier
                        and handle.execution_id not in selected
                    ):
                        selected[handle.execution_id] = handle
                        child_ids.add(handle.execution_id)
            frontier = child_ids
        return list(selected.values())

    async def _cancel_descendants(self, parent: SessionExecutionHandle) -> None:
        descendants = [
            handle
            for handle in self._with_descendants([parent])
            if handle.execution_id != parent.execution_id
        ]
        if not descendants:
            return
        for handle in descendants:
            handle.cancellation_requested = True
        await self._cancel_direct_handles(
            descendants,
            wait_timeout=self._cancel_timeout,
        )
        for handle in descendants:
            if not handle.state.terminal:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED
                )

    def _refresh_session_state(self, record: _SessionRecord) -> None:
        if self._sessions.get(record.session_id) is not record:
            return
        if record.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}:
            return
        active = self._registry.select(
            session_id=record.session_id,
            generation=record.generation,
            active_only=True,
        )
        record.state = RuntimeSessionState.ACTIVE if active else RuntimeSessionState.READY

    def _snapshot(self, record: _SessionRecord) -> RuntimeSessionSnapshot:
        return RuntimeSessionSnapshot(
            session_id=record.session_id,
            channel_id=record.channel_id,
            persistence_policy=record.persistence_policy,
            generation=record.generation,
            state=record.state,
            executions=self._registry.snapshots_for_session(
                record.session_id, generation=record.generation
            ),
        )
