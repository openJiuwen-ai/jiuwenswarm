# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import uuid
from typing import Any

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.session.stream import OutputSchema

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TODO_CLOSE_NUDGE_ID_PREFIX,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.server.runtime.agent_adapter.todo_close_text_hold import (
    TODO_CLOSE_DROP_STREAM_TYPE,
    TODO_CLOSE_HOLD_STREAM_TYPE,
    TODO_CLOSE_RELEASE_STREAM_TYPE,
)


class TodoCloseGate:
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self.used = False
        self._hold_pending = False
        self._undelivered: str | None = None

    def reset(self) -> None:
        self.used = False
        self._hold_pending = False
        self._undelivered = None

    async def arm(self, session: Any, *, can_nudge: bool) -> None:
        if session is None:
            return
        await self._retry_undelivered(session)
        if self._undelivered or self._hold_pending:
            return
        if self.used or not can_nudge:
            return
        if await self._write_control(session, TODO_CLOSE_HOLD_STREAM_TYPE):
            self._hold_pending = True

    def attach(self, response: Any, *, can_nudge: bool) -> bool:
        if self.used or response is None or not can_nudge:
            return False
        if getattr(response, "tool_calls", None):
            return False
        if str(getattr(response, "finish_reason", "") or "") == "length":
            return False
        if not str(getattr(response, "content", None) or "").strip():
            return False
        response.content = ""
        response.tool_calls = [
            ToolCall(
                id=f"{TODO_CLOSE_NUDGE_ID_PREFIX}{uuid.uuid4().hex[:12]}",
                type="function",
                name=self._tool_name,
                arguments="{}",
            )
        ]
        self.used = True
        return True

    async def finish(self, session: Any, *, dropped: bool) -> None:
        if session is None:
            self._hold_pending = False
            self._undelivered = None
            return
        if dropped or self._undelivered == "drop":
            if not self._hold_pending and self._undelivered != "drop":
                return
            wrote = await self._write_control(session, TODO_CLOSE_DROP_STREAM_TYPE)
            self._mark_delivered("drop", wrote)
            return
        if not self._hold_pending and self._undelivered != "release":
            return
        wrote = await self._write_control(session, TODO_CLOSE_RELEASE_STREAM_TYPE)
        self._mark_delivered("release", wrote)

    async def _retry_undelivered(self, session: Any) -> None:
        kind = self._undelivered
        if kind is None and self._hold_pending:
            kind = "release"
        if kind not in ("drop", "release"):
            return
        stream_type = TODO_CLOSE_DROP_STREAM_TYPE if kind == "drop" else TODO_CLOSE_RELEASE_STREAM_TYPE
        if await self._write_control(session, stream_type):
            self._undelivered = None
            self._hold_pending = False
            return
        self._undelivered = kind

    def _mark_delivered(self, kind: str, wrote: bool) -> None:
        if wrote:
            self._hold_pending = False
            self._undelivered = None
            return
        self._undelivered = kind

    @staticmethod
    async def _write_control(session: Any, stream_type: str) -> bool:
        try:
            await session.write_stream(
                OutputSchema(type=stream_type, index=0, payload={"result_type": "control"})
            )
        except Exception:  # noqa: BLE001
            logger.warning("[TodoCloseGate] todo close control %s failed", stream_type, exc_info=True)
            return False
        return True


__all__ = ["TodoCloseGate"]
