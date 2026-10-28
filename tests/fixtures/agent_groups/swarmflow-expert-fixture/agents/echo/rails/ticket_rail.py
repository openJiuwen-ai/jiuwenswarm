# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Smallest rail an AgentGroup member can carry: pin the echoed ticket to the task's own."""
from __future__ import annotations

import re

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

_TICKET = re.compile(r"ticket:\s*(\S+)", re.IGNORECASE)


class TicketRail(DeepAgentRail):
    """Rewrite the ticket argument to the one named in the task, so a typo cannot pass."""

    def __init__(self) -> None:
        super().__init__()
        self._ticket: str | None = None

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        match = _TICKET.search(str(getattr(ctx.inputs, "query", "") or ""))
        self._ticket = match.group(1) if match else None

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._ticket or ctx.inputs.tool_name != "echo_ticket":
            return
        if isinstance(ctx.inputs.tool_args, dict):
            ctx.inputs.tool_args["ticket"] = self._ticket
