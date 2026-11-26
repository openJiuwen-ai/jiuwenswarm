# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Batch adjacent text deltas for a single enterprise SSE chat response."""

from __future__ import annotations

import asyncio
from typing import Any

_FLUSH_SECONDS = 0.1
_MAX_CHARS = 16 * 1024


class PendingDelta:
    """One queued SSE frame. The reader waits for a boundary or the deadline."""

    def __init__(self, frame: dict[str, Any], text: str, deadline: float) -> None:
        self.frame = frame
        self.parts = [text]
        self.size = len(text)
        self.deadline = deadline
        self.ready = asyncio.Event()

    async def read(self) -> dict[str, Any]:
        if not self.ready.is_set():
            try:
                async with asyncio.timeout_at(self.deadline):
                    await self.ready.wait()
            except TimeoutError:
                pass
        self.ready.set()
        return {**self.frame, "payload": {**self.frame["payload"], "content": "".join(self.parts)}}


class ChatDeltaWindow:
    """Keep the first delta immediate and preserve order across all senders."""

    def __init__(self) -> None:
        self._identity: tuple[Any, ...] | None = None
        self._deadline = 0.0
        self._pending: PendingDelta | None = None
        self._last_order: int | None = None

    def finish(self) -> None:
        if self._pending is not None:
            self._pending.ready.set()
            self._pending = None
        self._identity = None

    def push(self, frame: dict[str, Any]) -> dict[str, Any] | PendingDelta | None:
        payload = frame.get("payload")
        if not isinstance(payload, dict) or payload.get("event_type") not in ("chat.delta", "chat.reasoning"):
            self.finish()
            return frame
        text = payload.get("content")
        bounded_text = isinstance(text, str) and 0 < len(text) <= _MAX_CHARS
        has_cursor = "runtime_event" in (frame.get("metadata") or {}) or "runtime_event" in payload
        if frame["is_complete"] or has_cursor or not bounded_text:
            self.finish()
            return frame

        fields = {key: value for key, value in payload.items() if key != "content"}
        order = fields.get("output_order")
        sequence = None
        if order is not None:
            # Live phase deltas have a new timestamp and order per token. Keep
            # the first token's placement, as the history segment writer does.
            if not (
                fields.get("output_phase_id")
                and isinstance(order, dict)
                and order.keys() == {"request_id", "sequence"}
                and order["request_id"] == frame["request_id"]
                and type(order["sequence"]) is int
            ):
                self.finish()
                return frame
            sequence = order["sequence"]
            del fields["output_order"]
            fields.pop("timestamp", None)

        identity = (frame["request_id"], frame["channel_id"], frame["metadata"], fields)
        consecutive = sequence is None and self._last_order is None
        if sequence is not None and self._last_order is not None:
            consecutive = sequence == self._last_order + 1
        self._last_order = sequence
        now = asyncio.get_running_loop().time()
        pending = self._pending
        if identity != self._identity or not consecutive or now >= self._deadline + _FLUSH_SECONDS:
            self.finish()
            self._identity = identity
            self._deadline = now + _FLUSH_SECONDS
            return frame
        batch_full = pending is not None and (pending.ready.is_set() or pending.size + len(text) > _MAX_CHARS)
        if now >= self._deadline or batch_full:
            if pending is not None:
                pending.ready.set()
            self._deadline = now + _FLUSH_SECONDS
            pending = None
        if pending is None:
            self._pending = PendingDelta(frame, text, self._deadline)
            return self._pending
        pending.parts.append(text)
        pending.size += len(text)
        return None
