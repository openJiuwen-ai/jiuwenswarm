# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process delivery of committed trajectory revision hints."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.observability.models import CommittedTraceUpdate

logger = logging.getLogger(__name__)

GatewayPushSender = Callable[[dict[str, Any]], Awaitable[bool | None]]
_RETRY_DELAY_SECONDS = 0.1


class TrajectoryGatewayHintBridge:
    """Move writer-thread commits onto the AgentServer-to-Gateway socket.

    The trajectory SQLite writer and browser WebSocket channel live in
    different processes. The process-local update broker therefore cannot
    wake the Gateway. This bridge carries only revision watermarks over the
    existing AgentServer push connection; SQLite remains the source of truth.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sender: GatewayPushSender | None = None
        self._pending: dict[tuple[str, str], CommittedTraceUpdate] = {}
        self._drain_task: asyncio.Task[None] | None = None

    def bind(
        self,
        loop: asyncio.AbstractEventLoop,
        sender: GatewayPushSender,
    ) -> None:
        """Bind the AgentServer event loop and its existing push sender."""
        with self._lock:
            self._loop = loop
            self._sender = sender

    async def unbind(self) -> None:
        """Stop future delivery and cancel the loop-owned drain task."""
        with self._lock:
            self._loop = None
            self._sender = None
            self._pending.clear()
            task = self._drain_task
            self._drain_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def publish(self, updates: tuple[CommittedTraceUpdate, ...]) -> None:
        """Coalesce committed hints and schedule a lossless loop-side drain."""
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            for update in updates:
                session_id = str(update.session_id or "").strip()
                trace_id = str(update.trace_id or "").strip()
                if not session_id or not trace_id:
                    continue
                key = (session_id, trace_id)
                current = self._pending.get(key)
                if current is None or update.revision >= current.revision:
                    self._pending[key] = update
            if not self._pending:
                return
        loop.call_soon_threadsafe(self._ensure_drain)

    def _ensure_drain(self) -> None:
        """Create at most one sender task on the bound event loop."""
        task = self._drain_task
        if task is None or task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Keep draining revisions that arrive while an earlier send is in flight."""
        try:
            while True:
                with self._lock:
                    sender = self._sender
                    if sender is None or not self._pending:
                        return
                    updates = tuple(self._pending.values())
                    self._pending.clear()
                retry_needed = False
                for update in updates:
                    try:
                        delivered = await sender(_push_message(update))
                    except Exception:
                        logger.exception(
                            "Trajectory revision hint push failed: session_id=%s trace_id=%s revision=%d",
                            update.session_id,
                            update.trace_id,
                            update.revision,
                        )
                        delivered = False
                    if delivered is False:
                        self._requeue(update)
                        retry_needed = True
                if retry_needed:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
        finally:
            self._drain_task = None
            with self._lock:
                loop = self._loop
                has_pending = bool(self._pending)
            if has_pending and loop is not None and not loop.is_closed():
                loop.call_soon(self._ensure_drain)

    def _requeue(self, update: CommittedTraceUpdate) -> None:
        """Keep the newest unsent watermark until the Gateway reconnects."""
        key = (update.session_id, update.trace_id)
        with self._lock:
            current = self._pending.get(key)
            if current is None or update.revision >= current.revision:
                self._pending[key] = update


def _push_message(update: CommittedTraceUpdate) -> dict[str, Any]:
    return {
        "request_id": (
            f"trajectory:{update.session_id}:{update.trace_id}:{update.revision}"
        ),
        "channel_id": "web",
        "session_id": update.session_id,
        "payload": {
            "event_type": "trace.updated",
            "session_id": update.session_id,
            "trace_id": update.trace_id,
            "revision": update.revision,
            "store_epoch": update.store_epoch,
            "lifecycle": update.lifecycle,
        },
        "is_complete": False,
    }


trajectory_gateway_hint_bridge = TrajectoryGatewayHintBridge()


__all__ = [
    "TrajectoryGatewayHintBridge",
    "trajectory_gateway_hint_bridge",
]
