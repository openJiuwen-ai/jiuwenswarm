"""Consume exact native confirmations in the Persistent employee execution host."""

from typing import Any

from openjiuwen.core.single_agent.interrupt.response import InterruptKind
from openjiuwen.core.single_agent.interrupt.state import InterruptAnswer

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail


class EmployeeAutoPermissionRail(AutoPermissionInterruptRail):
    """Use Core's verified, owner-scoped answers without the personal-chat queue."""

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
