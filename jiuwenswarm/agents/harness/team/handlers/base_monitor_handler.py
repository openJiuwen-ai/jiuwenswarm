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
        self._event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._collect_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start monitoring: start the underlying monitor and spawn the collect task."""
        if self._running:
            return
        await self._monitor.start()
        self._running = True
        self._collect_task = asyncio.create_task(self._collect_events())

    async def stop(self) -> None:
        """Stop monitoring: stop the underlying monitor and wait for the collect task to drain."""
        if not self._running:
            return
        self._running = False
        # Yield to the event loop so the collect task can process any events already
        # in the monitor queue before the sentinel is sent by monitor.stop().
        await asyncio.sleep(0)
        await self._monitor.stop()
        if self._collect_task is not None:
            try:
                await asyncio.wait_for(self._collect_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._collect_task.cancel()
                try:
                    await self._collect_task
                except asyncio.CancelledError:
                    pass
            except Exception as exc:
                logger.warning("[BaseMonitorHandler] collect task join failed: %s", exc)
            self._collect_task = None
        self._event_queue.put_nowait(None)

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
