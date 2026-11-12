# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Join one usage event with its call-end artifact in either SDK event order."""
from __future__ import annotations

from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponseChunk


class LlmUsageProjection:
    """Retain at most one usage envelope; missing end events still drain normally."""

    def __init__(self) -> None:
        self._refs: dict[str, Any] | None = None
        self._pending: AgentResponseChunk | None = None
        self._end_ready = False

    def start(self, payload: dict[str, Any]) -> AgentResponseChunk | None:
        pending = self.drain()
        refs = payload.get("payload_refs")
        self._refs = refs if isinstance(refs, dict) else None
        self._end_ready = False
        return pending

    def end(self, payload: dict[str, Any]) -> AgentResponseChunk | None:
        refs = payload.get("payload_refs")
        self._refs = refs if isinstance(refs, dict) else None
        pending = self.drain()
        self._end_ready = pending is None
        if pending is not None:
            self._refs = None
        return pending

    def accept(self, chunk: AgentResponseChunk) -> AgentResponseChunk | None:
        if self._end_ready:
            ready = self._attach_refs(chunk)
            self._end_ready = False
            self._refs = None
            return ready
        previous = self.drain()
        self._pending = chunk
        return previous

    def drain(self) -> AgentResponseChunk | None:
        pending, self._pending = self._pending, None
        return self._attach_refs(pending) if pending is not None else None

    def _attach_refs(self, chunk: AgentResponseChunk) -> AgentResponseChunk:
        metadata = chunk.payload.get("metadata")
        usage = metadata.get("usage_metadata") if isinstance(metadata, dict) else None
        if self._refs and isinstance(usage, dict):
            chunk.payload["metadata"] = {
                **metadata, "usage_metadata": {**usage, "payload_refs": self._refs},
            }
        return chunk

