# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front-owned in-flight request table.

Phase 1 keeps execution in-process. The registry still owns request IDs so
queued chat can be cancelled before Runtime is ``AGENT_READY``, and so
cancel/timeout have a single Front-side owner.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest


@dataclass
class TrackedRequest:
    request: AgentRequest
    queued: bool = True
    created_at: float = field(default_factory=time.monotonic)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def request_id(self) -> str:
        return str(self.request.request_id or "")

    @property
    def session_id(self) -> str:
        return str(self.request.session_id or "")


class RequestRegistry:
    """Track Front-accepted requests until they complete or cancel."""

    def __init__(self) -> None:
        self._items: dict[str, TrackedRequest] = {}
        self._lock = asyncio.Lock()

    async def register(self, request: AgentRequest, *, queued: bool = False) -> TrackedRequest:
        tracked = TrackedRequest(request=request, queued=queued)
        request_id = tracked.request_id
        if not request_id:
            return tracked
        async with self._lock:
            self._items[request_id] = tracked
        return tracked

    async def mark_dispatched(self, request_id: str) -> None:
        async with self._lock:
            tracked = self._items.get(request_id)
            if tracked is not None:
                tracked.queued = False

    async def complete(self, request_id: str) -> TrackedRequest | None:
        async with self._lock:
            return self._items.pop(request_id, None)

    async def cancel(self, request_id: str) -> TrackedRequest | None:
        async with self._lock:
            tracked = self._items.get(request_id)
        if tracked is None:
            return None
        tracked.cancelled.set()
        return tracked

    async def cancel_session(self, session_id: str) -> list[TrackedRequest]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        async with self._lock:
            matches = [
                item
                for item in self._items.values()
                if item.session_id == sid and not item.cancelled.is_set()
            ]
        cancelled: list[TrackedRequest] = []
        for item in matches:
            item.cancelled.set()
            cancelled.append(item)
        return cancelled

    def get(self, request_id: str) -> TrackedRequest | None:
        return self._items.get(request_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "request_id": item.request_id,
                "session_id": item.session_id,
                "method": (
                    item.request.req_method.value if item.request.req_method else ""
                ),
                "queued": item.queued,
                "cancelled": item.cancelled.is_set(),
            }
            for item in self._items.values()
        ]
