# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front-owned in-flight request table.

The registry owns request IDs on the Front side so queued chat can be
cancelled before the Worker is ``AGENT_READY``. The Worker owns the active
execution and its cancel token after dispatch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.ipc.protocol import request_scope


class DuplicateRequestError(ValueError):
    """An active request already owns this caller-scoped request ID."""


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
        self._items: dict[tuple[str, ...], TrackedRequest] = {}
        self._lock = asyncio.Lock()

    async def register(self, request: AgentRequest, *, queued: bool = False) -> TrackedRequest:
        tracked = TrackedRequest(request=request, queued=queued)
        request_id = tracked.request_id
        if not request_id:
            return tracked
        async with self._lock:
            key = (*request_scope(request)[:4], request_id)
            if key in self._items:
                raise DuplicateRequestError("duplicate active request_id")
            self._items[key] = tracked
        return tracked

    async def mark_dispatched(self, request_id: str, *, tracked: TrackedRequest | None = None) -> None:
        async with self._lock:
            current = tracked or self.get(request_id)
            if current is not None:
                current.queued = False

    async def complete(self, request_id: str, *, tracked: TrackedRequest | None = None) -> TrackedRequest | None:
        async with self._lock:
            current = tracked or self.get(request_id)
            if current is None:
                return None
            key = (*request_scope(current.request)[:4], request_id)
            if self._items.get(key) is current:
                return self._items.pop(key)
            return None

    async def cancel(self, request_id: str, *, owner: AgentRequest | None = None) -> TrackedRequest | None:
        async with self._lock:
            tracked = self.get(request_id, owner=owner)
        if tracked is None:
            return None
        tracked.cancelled.set()
        return tracked

    async def cancel_session(self, session_id: str, *, owner: AgentRequest | None = None) -> list[TrackedRequest]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        async with self._lock:
            candidates = [item for item in self._items.values() if item.session_id == sid]
            matches = []
            for item in candidates:
                if item.cancelled.is_set():
                    continue
                if owner is None or request_scope(item.request)[:4] == request_scope(owner)[:4]:
                    matches.append(item)
        cancelled: list[TrackedRequest] = []
        for item in matches:
            item.cancelled.set()
            cancelled.append(item)
        return cancelled

    def get(self, request_id: str, *, owner: AgentRequest | None = None) -> TrackedRequest | None:
        candidates = [item for item in self._items.values() if item.request_id == request_id]
        matches = [item for item in candidates
                   if owner is None or request_scope(item.request)[:4] == request_scope(owner)[:4]]
        return matches[0] if len(matches) == 1 else None

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
