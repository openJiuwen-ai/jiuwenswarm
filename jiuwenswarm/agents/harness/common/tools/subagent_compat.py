# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility fixes for the OpenJiuWen persistent subagent runtime."""

from __future__ import annotations

import logging
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
# list — all of those stay on the child session.
_MIRROR_CHUNK_TYPES = frozenset({"llm_reasoning", "tool_call", "tool_update", "tool_result"})
# A child task_id would open a phantom task segment on the parent stream.
_MIRROR_STRIP_KEYS = ("task_id", "parent_request_id")


class CompatibleSubagentControl(SubagentControl):
    """Report a restored subagent as idle until its next turn is submitted."""

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

    async def _on_child_chunk(self, subagent_id: str, chunk: Any) -> None:
        """Replay a child stream chunk onto the parent session stream.

        The mirrored frame carries ``stream_source_id = subagent_id`` so hosts
        that route by source (RelayClaw) nest it under the child instead of
        mixing it into the parent bubble. A mirror failure must never break
        the child turn, so write errors are logged and swallowed.
        """
        if not self.mirror_child_stream or self._parent_session is None:
            return
        chunk_type = getattr(chunk, "type", None)
        if chunk_type not in _MIRROR_CHUNK_TYPES:
            return
        raw = getattr(chunk, "payload", None)
        payload = dict(raw) if isinstance(raw, dict) else {"content": str(raw or "")}
        for key in _MIRROR_STRIP_KEYS:
            payload.pop(key, None)
        payload["stream_source_id"] = subagent_id
        try:
            await self._parent_session.write_stream(
                OutputSchema(type=chunk_type, index=0, payload=payload)
            )
        except Exception as exc:  # noqa: BLE001 — mirror must not break the child turn
            logger.warning(
                "[subagent_mirror] write failed subagent=%s type=%s: %s",
                subagent_id,
                chunk_type,
                exc,
            )


def install_subagent_control_compat_patch(*, mirror_child_stream: bool = False) -> None:
    """Use the compatible control for newly created parent-session runtimes."""
    CompatibleSubagentControl.mirror_child_stream = bool(mirror_child_stream)
    _control_registry.SubagentControl = CompatibleSubagentControl


__all__ = [
    "CompatibleSubagentControl",
    "install_subagent_control_compat_patch",
]
