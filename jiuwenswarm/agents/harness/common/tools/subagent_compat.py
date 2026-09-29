# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility fixes for the OpenJiuWen persistent subagent runtime."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from openjiuwen.core.session.stream.base import (
    OutputSchema,  # type: ignore[import-untyped]
)
from openjiuwen.harness.subagent_runtime.control import (  # type: ignore[import-untyped]
    SubagentControl,
)
from openjiuwen.harness.subagent_runtime.models import (  # type: ignore[import-untyped]
    ResumeResult,
    SubagentStatus,
    SubagentStatusKind,
)
from openjiuwen.harness.tools.subagent import (
    _control_registry,  # type: ignore[import-untyped]
)

logger = logging.getLogger(__name__)

# Child chunks safe to replay onto the parent session stream. ``answer``
# becomes chat.final, ``content_chunk`` becomes chat.delta and flips the
# parent's streamed-content flag, ``error`` fails the whole parent turn, and
# the child's own task.* / todo.updated frames would pollute the parent task
# list — all of those stay on the child session. ``tool_update`` is one
# progress frame per call; RelayClaw drops it outside team mode, so mirroring
# it only adds parent-socket traffic.
_MIRROR_CHUNK_TYPES = frozenset({"llm_reasoning", "tool_call", "tool_result"})
# A child task_id would open a phantom task segment on the parent stream.
_MIRROR_STRIP_KEYS = ("task_id", "parent_request_id")
# Token-level reasoning from several page subagents overflows the parent
# websocket and trips keepalive. Coalesce the original text, do not summarize.
_REASONING_FLUSH_CHARS = 200
_REASONING_FLUSH_INTERVAL_S = 0.3
_REASONING_FRAME_CAP = 2000


def _child_reasoning_text(payload: object) -> str:
    """Return the reasoning string from an openjiuwen or SkillTurbo chunk."""
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, str) and content:
        return content
    output = payload.get("output")
    if isinstance(output, str):
        return output
    return ""


class CompatibleSubagentControl(SubagentControl):
    """Report a restored subagent as idle until its next turn is submitted."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._mirror_reasoning_text: dict[str, str] = {}
        self._mirror_reasoning_flushed_at: dict[str, float] = {}
        self._mirror_reasoning_frames: dict[str, int] = {}

    async def resume(self, subagent_id: str) -> ResumeResult:
        result = await super().resume(subagent_id)
        if (
            not result.restored
            or result.status.kind is not SubagentStatusKind.PENDING_INIT
        ):
            return result

        instance = self._manager.find(subagent_id)  # pylint: disable=protected-access
        if instance is None:
            return result

        current = instance.agent_status()
        if current.kind is not SubagentStatusKind.PENDING_INIT:
            return replace(result, status=current)

        # OpenJiuWen represents a live, input-ready instance with a terminal
        # turn status; COMPLETED is mapped to the public ``idle`` state.
        idle = SubagentStatus.completed()
        await instance.status.set(idle)
        return replace(result, status=idle)

    # Toggled by ``install_subagent_control_compat_patch`` from
    # ``react.subagent_runtime.mirror_child_stream``. Class-level because the
    # control registry constructs instances itself.
    mirror_child_stream: bool = False

    def _reasoning_frames_full(self, subagent_id: str) -> bool:
        sent = self._mirror_reasoning_frames.get(subagent_id, 0)
        return sent >= _REASONING_FRAME_CAP

    def _reasoning_should_flush(self, subagent_id: str, text: str, now: float) -> bool:
        if len(text) >= _REASONING_FLUSH_CHARS:
            return True
        started = self._mirror_reasoning_flushed_at[subagent_id]
        return now - started >= _REASONING_FLUSH_INTERVAL_S

    async def _flush_child_reasoning(self, subagent_id: str) -> None:
        text = self._mirror_reasoning_text.pop(subagent_id, "")
        self._mirror_reasoning_flushed_at[subagent_id] = time.monotonic()
        if not text or self._reasoning_frames_full(subagent_id):
            return
        sent = self._mirror_reasoning_frames.get(subagent_id, 0)
        self._mirror_reasoning_frames[subagent_id] = sent + 1
        await self._write_mirrored_chunk(subagent_id, "llm_reasoning", {"content": text})

    async def _buffer_child_reasoning(self, subagent_id: str, chunk: Any) -> None:
        if self._reasoning_frames_full(subagent_id):
            return
        text = _child_reasoning_text(getattr(chunk, "payload", None))
        if not text:
            return
        merged = self._mirror_reasoning_text.get(subagent_id, "") + text
        self._mirror_reasoning_text[subagent_id] = merged
        now = time.monotonic()
        if subagent_id not in self._mirror_reasoning_flushed_at:
            self._mirror_reasoning_flushed_at[subagent_id] = now
        if not self._reasoning_should_flush(subagent_id, merged, now):
            return
        await self._flush_child_reasoning(subagent_id)

    async def _write_mirrored_chunk(
        self,
        subagent_id: str,
        chunk_type: str,
        payload: dict[str, Any],
    ) -> None:
        body = dict(payload)
        for key in _MIRROR_STRIP_KEYS:
            body.pop(key, None)
        body["stream_source_id"] = subagent_id
        try:
            await self._parent_session.write_stream(
                OutputSchema(type=chunk_type, index=0, payload=body)
            )
        except Exception as exc:  # noqa: BLE001 — mirror must not break the child turn
            logger.warning(
                "[subagent_mirror] write failed subagent=%s type=%s: %s",
                subagent_id,
                chunk_type,
                exc,
            )

    async def _on_child_chunk(self, subagent_id: str, chunk: Any) -> None:
        """Replay a child stream chunk onto the parent session stream.

        Reasoning is coalesced per child (200 characters or 300ms) so several
        page subagents do not flood the parent websocket. Tool calls and
        results are still copied in full. The mirrored frame carries
        ``stream_source_id = subagent_id`` so hosts that route by source
        (RelayClaw) nest it under the child. A mirror failure must never
        break the child turn.
        """
        if not self.mirror_child_stream or self._parent_session is None:
            return
        chunk_type = getattr(chunk, "type", None)
        if chunk_type == "llm_reasoning":
            await self._buffer_child_reasoning(subagent_id, chunk)
            return
        await self._flush_child_reasoning(subagent_id)
        if chunk_type not in _MIRROR_CHUNK_TYPES:
            return
        raw = getattr(chunk, "payload", None)
        payload = dict(raw) if isinstance(raw, dict) else {"content": str(raw or "")}
        await self._write_mirrored_chunk(subagent_id, str(chunk_type), payload)


def install_subagent_control_compat_patch(*, mirror_child_stream: bool = False) -> None:
    """Use the compatible control for newly created parent-session runtimes."""
    CompatibleSubagentControl.mirror_child_stream = bool(mirror_child_stream)
    _control_registry.SubagentControl = CompatibleSubagentControl


__all__ = [
    "CompatibleSubagentControl",
    "install_subagent_control_compat_patch",
]
