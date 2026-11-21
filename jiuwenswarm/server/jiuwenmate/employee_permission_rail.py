"""Consume exact native confirmations in the Persistent employee execution host."""

import json
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage

from openjiuwen.core.single_agent.interrupt.response import InterruptKind
from openjiuwen.core.single_agent.interrupt.state import InterruptAnswer

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail


class EmployeeAutoPermissionRail(AutoPermissionInterruptRail):
    """Use Core's verified, owner-scoped answers without the personal-chat queue."""

    def __init__(self, *, swarmchat: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._swarmchat = swarmchat

    async def before_tool_call(self, ctx: Any) -> Any:
        if self._swarmchat and getattr(ctx.inputs, "tool_name", None) == "bash":
            from jiuwenswarm.server.jiuwenmate.swarmchat.port import feishu_roster_cli_redirect

            args = getattr(ctx.inputs, "tool_args", None)
            call = ctx.inputs.tool_call
            if not isinstance(args, dict) and call is not None:
                try:
                    args = json.loads(call.arguments)
                except (ValueError, TypeError):
                    args = None
            if call is not None and isinstance(args, dict):
                output = feishu_roster_cli_redirect(args.get("command"))
                if output is not None:
                    ctx.extra.setdefault("_skip_tool_calls", {})[call.id] = True
                    ctx.inputs.tool_result = output
                    ctx.inputs.tool_msg = ToolMessage(
                        content=json.dumps(output.data, ensure_ascii=False), tool_call_id=call.id,
                    )
                    return None
        return await super().before_tool_call(ctx)

    def _native_permission_answer(self, ctx: Any, tool_call_id: str | None) -> InterruptAnswer | None:
        if ctx is None or not tool_call_id:
            return None
        answer = self.base_rail.find_own_answer(ctx.extra, tool_call_id)
        owner = self.base_rail.interrupt_owner
        if not isinstance(answer, InterruptAnswer) or answer.kind != InterruptKind.PERMISSION:
            return None
        if answer.owner != owner:
            return None
        request = answer.request
        if request.interrupt_owner != owner or request.interrupt_kind != InterruptKind.PERMISSION:
            return None
        return answer
