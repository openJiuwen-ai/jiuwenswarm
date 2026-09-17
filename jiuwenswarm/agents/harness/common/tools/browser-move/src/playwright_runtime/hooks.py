#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Browser runtime hook and middleware helpers."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from openjiuwen.core.single_agent.middleware.base import AgentCallbackContext, AgentMiddleware


class BrowserRunCancelled(RuntimeError):
    """Raised when a browser run is canceled by external signal."""


class BrowserCancellationMiddleware(AgentMiddleware):
    """Abort browser agent execution when cancellation flag is set."""

    priority = 1

    def __init__(
        self,
        is_cancelled: Callable[[str, Optional[str]], Awaitable[bool]],
    ) -> None:
        self._is_cancelled = is_cancelled

    async def _abort_if_cancelled(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs if isinstance(ctx.inputs, dict) else {}
        session_id = str(inputs.get("conversation_id", "")).strip()
        request_id_raw = str(inputs.get("request_id", "")).strip()
        request_id = request_id_raw if request_id_raw else None
        if not session_id:
            return
        if await self._is_cancelled(session_id, request_id):
            raise BrowserRunCancelled(
                f"browser run canceled (session_id={session_id}, request_id={request_id or ''})"
            )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._abort_if_cancelled(ctx)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._abort_if_cancelled(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._abort_if_cancelled(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        # This fires after each tool step, enabling prompt stop after frontend disconnect.
        await self._abort_if_cancelled(ctx)
