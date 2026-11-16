# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Order model usage after call-end and retain pending usage for audit."""
from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentResponseChunk


class LlmUsageProjection:
    """Retain at most one usage envelope; missing end events still drain normally."""

    def __init__(self) -> None:
        self._pending: AgentResponseChunk | None = None
        self._end_ready = False

    def start(self) -> AgentResponseChunk | None:
        pending = self.drain()
        self._end_ready = False
        return pending

    def end(self) -> None:
        # Retain pending usage while the adapter yields the end marker. A
        # consumer can close at that yield before accepting the usage itself.
        self._end_ready = True

    def accept(self, chunk: AgentResponseChunk) -> AgentResponseChunk | None:
        if self._end_ready:
            self._end_ready = False
            return chunk
        previous = self.drain()
        self._pending = chunk
        return previous

    def drain(self) -> AgentResponseChunk | None:
        pending, self._pending = self._pending, None
        if pending is None:
            return None
        self._end_ready = False
        return pending
