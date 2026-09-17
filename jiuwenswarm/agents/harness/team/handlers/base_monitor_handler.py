# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Base class for TeamMonitor-backed event handlers.

Provides the shared lifecycle (start/stop), event queue, and events() iterator.
Subclasses implement _collect_events() to consume the appropriate monitor stream.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

from openjiuwen.agent_teams.monitor import TeamMonitor

logger = logging.getLogger(__name__)

MONITOR_EVENT_QUEUE_MAXSIZE = 64


class DropOldestQueue(asyncio.Queue):
    """Bounded queue that keeps recent state without evicting messages first.

    State notifications are reconstructable from the terminal team snapshot,
    while ``team.message`` events are not.  When the queue is full, discard the
    oldest reconstructable event first.  If every queued item is protected, the
    oldest item is still discarded so memory remains strictly bounded.
    """

    def __init__(self, maxsize: int = MONITOR_EVENT_QUEUE_MAXSIZE) -> None:
        super().__init__(maxsize=maxsize)
        self.dropped_count = 0

    @staticmethod
    def _is_protected(item: Any) -> bool:
        # A stop sentinel must always enter a full queue.  Monitor message and
        # broadcast events cannot be recreated by the final member/task state
        # snapshot, so prefer them over coalescible state notifications.
        if item is None:
            return True
        if isinstance(item, dict):
            return item.get("event_type") == "team.message"
        event_type = getattr(item, "event_type", None)
        event_type_value = getattr(event_type, "value", event_type)
        return str(event_type_value or "").strip().lower() in {
            "message",
            "broadcast",
            "team.message",
        }

    def _discard_queued_item(self, index: int) -> None:
        del self._queue[index]
        self.dropped_count += 1
        # The discarded item can never receive task_done().  Keep the inherited
        # Queue accounting correct for callers that use join().
        self.task_done()

    def put_nowait(self, item: Any) -> None:
        if self.full():
            drop_index = next(
                (
                    index
                    for index, queued_item in enumerate(self._queue)
                    if not self._is_protected(queued_item)
                ),
                None,
            )
            if drop_index is not None:
                self._discard_queued_item(drop_index)
            elif self._is_protected(item):
                self._discard_queued_item(0)
            else:
                # Do not evict a non-reconstructable message for a state delta.
                self.dropped_count += 1
                return
        super().put_nowait(item)

    async def put(self, item: Any) -> None:
        self.put_nowait(item)


class BaseMonitorHandler:
    """Shared lifecycle for handlers that wrap a TeamMonitor.

    Subclasses must implement _collect_events() to consume from the monitor
    (e.g. monitor.events() or monitor.workflow_events()) and put processed
    dicts onto self._event_queue.

    Lifecycle:
        handler = SubclassHandler(monitor, session_id)
        await handler.start()
        async for event in handler.events():
            ...
        await handler.stop()
    """

    def __init__(self, monitor: TeamMonitor, session_id: str) -> None:
        self._monitor = monitor
        self._session_id = session_id
        self._event_queue: DropOldestQueue = DropOldestQueue()
        self._collect_task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False
        self._bound_monitor_source_queues()

    def _bound_monitor_source_queues(self) -> None:
        """Bound both SDK source queues, including the one this handler ignores.

        ``TeamMonitor`` always subscribes to regular and workflow events, while
        each local handler consumes only one of those streams.  Without this
        guard, the unused SDK queue grows for the entire session.
        """
        for attr_name in ("_event_queue", "_workflow_event_queue"):
            source_queue = getattr(self._monitor, attr_name, None)
            if not isinstance(source_queue, asyncio.Queue) or source_queue.maxsize > 0:
                continue
            bounded_queue = DropOldestQueue()
            while True:
                try:
                    bounded_queue.put_nowait(source_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            setattr(self._monitor, attr_name, bounded_queue)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_consumer_task(self, task: asyncio.Task) -> None:
        """Register the single task that drains ``events()`` for this handler."""
        previous = self._consumer_task
        if previous is not None and previous is not task and not previous.done():
            task.cancel()
            raise RuntimeError("monitor consumer task is already running")
        self._consumer_task = task

    async def _stop_consumer_task(self) -> None:
        task = self._consumer_task
        self._consumer_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[BaseMonitorHandler] consumer task join failed: %s", exc)

    async def start(self) -> None:
        """Start monitoring: start the underlying monitor and spawn the collect task."""
        if self._running:
            return
        await self._monitor.start()
        self._running = True
        self._collect_task = asyncio.create_task(self._collect_events())

    async def stop(self) -> None:
        """Stop monitoring: stop the underlying monitor and wait for the collect task to drain."""
        if not self._running and self._collect_task is None and self._consumer_task is None:
            return
        self._running = False
        try:
            # Yield to the event loop so the collect task can process any events already
            # in the monitor queue before the sentinel is sent by monitor.stop().
            await asyncio.sleep(0)
            await self._monitor.stop()
            if self._collect_task is not None:
                await asyncio.wait_for(self._collect_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "[BaseMonitorHandler] collect task stop timed out: session_id=%s",
                self._session_id,
            )
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            logger.warning(
                "[BaseMonitorHandler] collect task was cancelled before stop: "
                "session_id=%s",
                self._session_id,
            )
        except Exception as exc:
            logger.warning("[BaseMonitorHandler] monitor stop failed: %s", exc)
        finally:
            if self._collect_task is not None and not self._collect_task.done():
                self._collect_task.cancel()
                try:
                    await self._collect_task
                except asyncio.CancelledError:
                    pass
            self._collect_task = None
            self._event_queue.put_nowait(None)
            await self._stop_consumer_task()

    # ------------------------------------------------------------------
    # Async iterator — events()
    # ------------------------------------------------------------------

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield event dicts from the internal queue.

        Terminates on the None sentinel placed by stop().
        Falls back to a timeout loop so callers polling while _running also exit
        cleanly if stop() is called between queue.get() calls.
        """
        while True:
            try:
                item = await asyncio.wait_for(self._event_queue.get(), timeout=0.1)
                if item is None:
                    break
                yield item
            except asyncio.TimeoutError:
                if not self._running:
                    break

    # ------------------------------------------------------------------
    # Abstract — subclasses must implement
    # ------------------------------------------------------------------

    async def _collect_events(self) -> None:
        """Consume from the monitor stream and push processed dicts to _event_queue.

        Called as an asyncio Task by start(). Exits naturally when the monitor
        stream terminates (stop() sends a None sentinel to the monitor queue).
        """
        raise NotImplementedError
