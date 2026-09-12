"""Per-Session LATEST_FIRST scheduler used by the Runtime coordinator."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from jiuwenswarm.runtime.session.model import SessionExecutionHandle

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _QueuedWork:
    handle: SessionExecutionHandle
    operation: Callable[[], Awaitable[Any]]
    context: contextvars.Context
    result: asyncio.Future[Any]


@dataclass(slots=True)
class _Lane:
    session_id: str
    generation: int
    queue: asyncio.PriorityQueue = field(default_factory=asyncio.PriorityQueue)
    priority: int = 0
    sequence: int = 0
    processor: asyncio.Task[None] | None = None
    queued: dict[str, _QueuedWork] = field(default_factory=dict)
    current: _QueuedWork | None = None
    current_task: asyncio.Task[Any] | None = None


class SessionWorkScheduler:
    """Runs one operation at a time per Session and Sessions in parallel."""

    def __init__(self) -> None:
        self._lanes: dict[str, _Lane] = {}
        self._closing_tasks: dict[tuple[str, int], set[asyncio.Task[Any]]] = {}
        self._closed = False

    async def submit_and_wait(
        self,
        handle: SessionExecutionHandle,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._closed:
            raise RuntimeError("session scheduler is closed")
        lane = self._ensure_lane(handle.session_id, handle.generation)
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()
        queued = _QueuedWork(handle, operation, contextvars.copy_context(), result)
        lane.priority -= 1
        lane.sequence += 1
        lane.queued[handle.execution_id] = queued
        await lane.queue.put((lane.priority, lane.sequence, queued))
        return await result

    async def cancel_handles(
        self,
        handles: list[SessionExecutionHandle],
        *,
        wait_timeout: float | None,
    ) -> tuple[str, ...]:
        wait_targets: dict[str, asyncio.Task[Any]] = {}
        execution_ids = {handle.execution_id for handle in handles}
        for lane in tuple(self._lanes.values()):
            current = lane.current
            if current is not None and current.handle.execution_id in execution_ids:
                if lane.current_task is not None and not lane.current_task.done():
                    lane.current_task.cancel()
                    wait_targets[current.handle.execution_id] = lane.current_task
        for handle in handles:
            if handle.execution_id not in wait_targets and handle.state.value == "queued":
                target_lane = self._lanes.get(handle.session_id)
                if (
                    target_lane is not None
                    and target_lane.generation == handle.generation
                ):
                    self._cancel_queued(target_lane, handle.execution_id)
        if not wait_targets:
            return ()
        done, pending = await asyncio.wait(
            set(wait_targets.values()), timeout=wait_timeout
        )
        for task in done:
            self._consume_task(task)
        return tuple(
            execution_id
            for execution_id, task in wait_targets.items()
            if task in pending
        )

    async def close_session(
        self,
        session_id: str,
        *,
        generation: int | None = None,
        wait_timeout: float | None = 5.0,
    ) -> tuple[bool, tuple[str, ...]]:
        lane = self._lanes.get(session_id)
        if lane is None or (generation is not None and lane.generation != generation):
            return False, ()
        if self._lanes.get(session_id) is lane:
            self._lanes.pop(session_id, None)
        self._drain_queued(lane)
        current_id = lane.current.handle.execution_id if lane.current else None
        if lane.processor is not None and not lane.processor.done():
            lane.processor.cancel()
        targets = {
            task
            for task in (lane.processor, lane.current_task)
            if task is not None and not task.done() and task is not asyncio.current_task()
        }
        if not targets:
            return True, ()
        key = (session_id, lane.generation)
        self._closing_tasks.setdefault(key, set()).update(targets)
        done, pending = await asyncio.wait(targets, timeout=wait_timeout)
        for task in done:
            self._finish_closing(key, task)
        timed_out = (current_id,) if pending and current_id is not None else ()
        return True, timed_out

    async def close(self, *, wait_timeout: float | None = 5.0) -> tuple[str, ...]:
        self._closed = True
        timed_out: list[str] = []
        for session_id, lane in tuple(self._lanes.items()):
            _existed, pending = await self.close_session(
                session_id,
                generation=lane.generation,
                wait_timeout=wait_timeout,
            )
            timed_out.extend(pending)
        closing: set[asyncio.Task[Any]] = set()
        for tasks in self._closing_tasks.values():
            for task in tasks:
                if not task.done():
                    closing.add(task)
        if closing:
            done, _pending = await asyncio.wait(closing, timeout=wait_timeout)
            for key, tasks in tuple(self._closing_tasks.items()):
                for task in tuple(tasks & done):
                    self._finish_closing(key, task)
        return tuple(dict.fromkeys(timed_out))

    def _ensure_lane(self, session_id: str, generation: int) -> _Lane:
        lane = self._lanes.get(session_id)
        if lane is not None:
            if lane.generation != generation:
                raise RuntimeError(
                    f"session generation mismatch: {session_id} "
                    f"expected={lane.generation} actual={generation}"
                )
            return lane
        lane = _Lane(session_id=session_id, generation=generation)
        self._lanes[session_id] = lane
        lane.processor = asyncio.create_task(self._process(lane))
        return lane

    async def _process(self, lane: _Lane) -> None:
        processor = asyncio.current_task()
        try:
            while True:
                if self._lanes.get(lane.session_id) is not lane:
                    return
                _priority, _sequence, queued = await lane.queue.get()
                lane.queued.pop(queued.handle.execution_id, None)
                lane.current = queued
                if queued.result.cancelled():
                    lane.current = None
                    lane.queue.task_done()
                    continue
                task = asyncio.create_task(
                    queued.operation(), context=queued.context
                )
                lane.current_task = task
                queued.handle.task = task
                try:
                    value = await task
                except asyncio.CancelledError:
                    if not queued.result.done():
                        queued.result.cancel()
                except BaseException as exc:
                    if not queued.result.done():
                        queued.result.set_exception(exc)
                else:
                    if not queued.result.done():
                        queued.result.set_result(value)
                finally:
                    queued.handle.task = None
                    lane.current = None
                    lane.current_task = None
                    lane.queue.task_done()
        except asyncio.CancelledError:
            current = lane.current_task
            if current is not None and not current.done():
                current.cancel()
                try:
                    await current
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            if self._lanes.get(lane.session_id) is lane:
                self._lanes.pop(lane.session_id, None)
            self._drain_queued(lane)
            if processor is not None:
                self._finish_closing((lane.session_id, lane.generation), processor)

    @staticmethod
    def _cancel_queued(lane: _Lane, execution_id: str) -> None:
        # PriorityQueue has no removal operation. Cancelling the result makes
        # the processor skip the item without executing it.
        queued = lane.queued.get(execution_id)
        if queued is not None and not queued.result.done():
            queued.result.cancel()

    @staticmethod
    def _drain_queued(lane: _Lane) -> None:
        while True:
            try:
                _priority, _sequence, queued = lane.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            lane.queued.pop(queued.handle.execution_id, None)
            if not queued.result.done():
                queued.result.cancel()
            lane.queue.task_done()

    def _finish_closing(
        self, key: tuple[str, int], task: asyncio.Task[Any]
    ) -> None:
        tasks = self._closing_tasks.get(key)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._closing_tasks.pop(key, None)
        self._consume_task(task)

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        if not task.done():
            return
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
