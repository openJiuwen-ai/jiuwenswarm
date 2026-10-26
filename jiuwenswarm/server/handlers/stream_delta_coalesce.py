# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把 Deep 外层流的增量按时间片合并后再写上 WebSocket。

复杂任务的外层模型推理原来一块一帧。团队流已在 team_helpers 合并，
这里不重复处理团队请求。第一块仍马上发出，连续增量攒满 1 秒再拼成一帧；
工具、任务和结束事件先把攒着的字冲出去，避免文字和这些事件错序。
并行页面用 stream_source_id 分桶，避免把不同来源拼进同一帧。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from jiuwenswarm.common.schema.agent import AgentResponseChunk

_DELTA_EVENT_TYPES = frozenset({"chat.delta", "chat.reasoning"})
_FLUSH_SECONDS = 1.0


@dataclass
class _HeldDelta:
    """同一来源上尚未写出的增量。"""

    template: AgentResponseChunk
    parts: list[str]
    since: float


class StreamDeltaCoalescer:
    """按来源把 chat.delta / chat.reasoning 收成较少的帧。"""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._buckets: dict[tuple[str, str, str, str], _HeldDelta] = {}

    def push(self, chunk: AgentResponseChunk) -> list[AgentResponseChunk]:
        """返回现在就该写出的帧。团队请求或非增量原样返回。"""
        if not self._enabled:
            return [chunk]
        if chunk.is_complete or not isinstance(chunk.payload, dict):
            return self._boundary(chunk)
        text = _delta_text(chunk.payload)
        if text is None:
            return self._boundary(chunk)
        now = time.monotonic()
        key = _bucket_key(chunk.payload)
        ready = self._take_expired(now, skip=key)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _HeldDelta(template=chunk, parts=[], since=now)
            ready.append(chunk)
            return ready
        if not bucket.parts:
            bucket.template = chunk
        bucket.parts.append(text)
        if now - bucket.since >= _FLUSH_SECONDS:
            merged = self._release(bucket)
            if merged is not None:
                ready.append(merged)
        return ready

    def flush(self) -> list[AgentResponseChunk]:
        """流结束时发出还攒着的增量。"""
        if not self._enabled:
            return []
        return self._take_all()

    def _boundary(self, chunk: AgentResponseChunk) -> list[AgentResponseChunk]:
        ready = self._take_all()
        ready.append(chunk)
        return ready

    def _take_expired(
        self,
        now: float,
        *,
        skip: tuple[str, str, str, str],
    ) -> list[AgentResponseChunk]:
        ready: list[AgentResponseChunk] = []
        for key in list(self._buckets):
            if key == skip:
                continue
            bucket = self._buckets.get(key)
            if bucket is None or not bucket.parts:
                continue
            if now - bucket.since < _FLUSH_SECONDS:
                continue
            merged = self._release(bucket)
            if merged is not None:
                ready.append(merged)
        return ready

    def _take_all(self) -> list[AgentResponseChunk]:
        ready: list[AgentResponseChunk] = []
        for key in list(self._buckets):
            bucket = self._buckets.get(key)
            if bucket is None:
                continue
            merged = self._release(bucket)
            if merged is not None:
                ready.append(merged)
        self._buckets.clear()
        return ready

    @staticmethod
    def _release(bucket: _HeldDelta) -> AgentResponseChunk | None:
        if not bucket.parts:
            return None
        content = "".join(bucket.parts)
        payload = dict(bucket.template.payload or {})
        payload["content"] = content
        merged = replace(bucket.template, payload=payload)
        bucket.parts = []
        bucket.since = time.monotonic()
        return merged


def _delta_text(payload: dict) -> str | None:
    kind = str(payload.get("event_type") or "")
    if kind not in _DELTA_EVENT_TYPES:
        return None
    text = payload.get("content")
    if not isinstance(text, str) or text == "":
        return None
    return text


def _bucket_key(payload: dict) -> tuple[str, str, str, str]:
    return (
        str(payload.get("event_type") or ""),
        str(payload.get("stream_source_id") or ""),
        str(payload.get("role") or ""),
        str(payload.get("member_name") or ""),
    )
