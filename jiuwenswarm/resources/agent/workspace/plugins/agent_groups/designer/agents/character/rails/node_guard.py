# -*- coding: utf-8 -*-
from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentRail


class NodeGuardRail(AgentRail):
    """Keep the shot ticket unchanged and allow the generate tool only once.

    This rail does not call request_force_finish. Ending the turn stays with
    the engine structured-output rail.
    """

    priority = 50

    def __init__(self) -> None:
        self._ticket: str | None = None
        self._generated = False

    async def before_tool_call(self, ctx) -> None:
        args = getattr(ctx.inputs, "tool_args", None)
        if not isinstance(args, dict):
            return
        ticket = args.get("shot_ticket")
        if isinstance(ticket, str) and ticket.strip():
            if self._ticket is None:
                self._ticket = ticket
            elif ticket != self._ticket:
                args["shot_ticket"] = self._ticket
        name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if name == "generate_character" and self._generated:
            args["__node_guard_reject__"] = "generate_already_called"

    async def after_tool_call(self, ctx) -> None:
        name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if name == "generate_character":
            self._generated = True
