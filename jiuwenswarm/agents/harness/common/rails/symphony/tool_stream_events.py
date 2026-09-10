# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming lifecycle support for Symphony orchestration tools."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common.tool_progress_context import (
    bind_tool_progress,
    reset_tool_progress,
)
from jiuwenswarm.common.utils import logger


class SymphonyToolStreamHandler:
    """Own Symphony-specific stream progress and completion behavior."""

    COMPOSE_TOOL_NAME = "symphony_compose_graph"
    _PROGRESS_TOKEN_KEY = "_symphony_tool_progress_token"
    _RESULT_FIELDS = (
        "graph_status",
        "graph_build",
    )

    @classmethod
    def matches(cls, tool_call: Any) -> bool:
        return cls._tool_name(tool_call) == cls.COMPOSE_TOOL_NAME

    def bind_progress(
        self,
        ctx: AgentCallbackContext,
        session: Session,
        tool_call: Any,
    ) -> None:
        if not self.matches(tool_call):
            return

        async def progress_callback(event: dict[str, Any]) -> None:
            await self._emit_progress(session, tool_call, event)

        ctx.extra[self._PROGRESS_TOKEN_KEY] = bind_tool_progress(progress_callback)

    def reset_progress(self, ctx: AgentCallbackContext) -> None:
        reset_tool_progress(ctx.extra.pop(self._PROGRESS_TOKEN_KEY, None))

    def enrich_result_payload(
        self,
        tool_call: Any,
        payload: dict[str, Any],
        raw_output: Any,
    ) -> None:
        if not self.matches(tool_call) or not isinstance(raw_output, dict):
            return
        for key in self._RESULT_FIELDS:
            if key in raw_output:
                payload[key] = raw_output[key]

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        return str(getattr(tool_call, "name", "") if tool_call else "").strip()

    @staticmethod
    async def _emit_progress(
        session: Session,
        tool_call: Any,
        event: dict[str, Any],
    ) -> None:
        if not isinstance(event.get("graph"), dict):
            return
        try:
            await session.write_stream(
                OutputSchema(
                    type="tool_update",
                    index=0,
                    payload={
                        "tool_update": {
                            "tool_name": getattr(tool_call, "name", ""),
                            "tool_call_id": getattr(tool_call, "id", ""),
                            "status": "in_progress",
                            "beam_search_event": event,
                        }
                    },
                )
            )
        except Exception:
            logger.debug(
                "Symphony tool progress emit failed",
                exc_info=True,
            )


__all__ = ["SymphonyToolStreamHandler"]
