# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hold outer-model chat text while a started todo is still open.

``llm_output`` is written to the session stream before ``after_model_call``.
When that hook replaces the turn with the todo-close nudge, those chunks
must not be forwarded: the frontend would keep them inside the open task
segment. The rail emits hold / drop / release around the model call; this
buffer is what the adapter applies.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.utils import logger

TODO_CLOSE_HOLD_STREAM_TYPE = "todo_close_hold"
TODO_CLOSE_DROP_STREAM_TYPE = "todo_close_drop"
TODO_CLOSE_RELEASE_STREAM_TYPE = "todo_close_release"

# 这些帧说明本轮模型输出已经定案，挂起的正文可以下发。
# 补刀作废走 drop，不会落到这里。
TODO_CLOSE_COMMIT_CHUNK_TYPES = frozenset({
    "tool_call",
    "answer",
    "task.complete",
})


def is_todo_close_control(chunk_type: str) -> bool:
    """True for the rail's hold / drop / release frames."""
    return chunk_type in (
        TODO_CLOSE_HOLD_STREAM_TYPE,
        TODO_CLOSE_DROP_STREAM_TYPE,
        TODO_CLOSE_RELEASE_STREAM_TYPE,
    )


class TodoCloseTextHold:
    """Buffer one model call's outer chat deltas until the rail decides."""

    def __init__(self) -> None:
        self.armed = False
        self._payloads: list[dict[str, Any]] = []

    def decide(self, chunk_type: str, *, foreign: bool = False) -> str:
        """What the adapter should do with this chunk.

        ``skip``: control frame already applied, do not forward.
        ``flush_skip``: release; emit the buffer and do not forward the frame.
        ``flush_pass``: a commit frame arrived before release; emit the buffer,
        then process this frame.
        ``buffer``: outer ``llm_output`` while armed.
        ``pass``: forward immediately, including child streams.
        """
        control = self.on_control(chunk_type)
        if control in ("arm", "drop"):
            return "skip"
        if control == "release":
            return "flush_skip"
        if self.should_flush_for(chunk_type):
            self.disarm()
            return "flush_pass"
        if chunk_type == "llm_output" and self.should_buffer(foreign):
            return "buffer"
        return "pass"

    def on_control(self, chunk_type: str) -> str:
        """Apply a control frame.

        Returns ``arm``, ``drop``, ``release``, or ``""``.
        A second hold drops whatever the previous call never released.
        """
        if chunk_type == TODO_CLOSE_HOLD_STREAM_TYPE:
            self._replace_hold()
            return "arm"
        if chunk_type == TODO_CLOSE_DROP_STREAM_TYPE:
            self._payloads.clear()
            self.armed = False
            return "drop"
        if chunk_type == TODO_CLOSE_RELEASE_STREAM_TYPE:
            self.armed = False
            return "release"
        return ""

    def should_buffer(self, foreign: bool) -> bool:
        """Outer-model text is held. Child streams keep flowing."""
        if foreign:
            return False
        return self.armed

    def push(self, payload: dict[str, Any]) -> None:
        self._payloads.append(payload)

    def should_flush_for(self, chunk_type: str) -> bool:
        """Commit frames flush text when the release frame was missed."""
        if chunk_type not in TODO_CLOSE_COMMIT_CHUNK_TYPES:
            return False
        return bool(self._payloads)

    def disarm(self) -> None:
        self.armed = False

    def take(self) -> list[dict[str, Any]]:
        payloads = self._payloads
        self._payloads = []
        return payloads

    def _replace_hold(self) -> None:
        if self._payloads:
            logger.warning(
                "[TodoCloseTextHold] replaced %d unreleased chat payload(s)",
                len(self._payloads),
            )
        self._payloads.clear()
        self.armed = True
