"""Attach CUA code continuations through the existing root rail extension."""

from __future__ import annotations

from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

from jiuwenswarm.agents.harness.common.rails.cua_delegation_approval_rail import CuaDelegationApprovalRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_NON_PERMISSION_RESUME_ATTRIBUTE,
    RootNonPermissionResume,
)

from .approval import CuaApprovalTool, CuaCodeApprovals


class CuaCodeApprovalRail(CuaDelegationApprovalRail):
    """Preserve stock delegation approval and retain only pending code cells."""

    def __init__(self, language="cn"):
        super().__init__(language=language)
        self._code_approvals = CuaCodeApprovals()

    def fork_for_agent(self):
        return CuaCodeApprovalRail(language=self._language)

    def callback_priority(self, event):
        if event == AgentCallbackEvent.BEFORE_TOOL_CALL:
            return 91
        return super().callback_priority(event)

    def init(self, agent):
        card = agent.ability_manager.get("task_tool")
        if card is None:
            return
        original = Runner.resource_mgr.get_tool(card.id)
        if original is None:
            return
        wrapped = CuaApprovalTool(original, card.id)
        agent.ability_manager.remove_ability("task_tool")
        agent.ability_manager.add_ability(wrapped.card, wrapped)
        agent.register_task_resource_cleanup(self._code_approvals.close)

    async def before_invoke(self, ctx):
        # A fresh request supersedes a suspended delegation. A normal approval
        # resumes the same child, namespace and still-unsent code cell.
        if not isinstance(ctx.inputs.query, InteractiveInput):
            await self._code_approvals.close()

    async def before_tool_call(self, ctx):
        if ctx.inputs.tool_name != "task_tool":
            await super().before_tool_call(ctx)
            return
        user_input = None
        if self._code_approvals.is_pending(ctx):
            user_input = self._get_user_input(ctx, ctx.inputs.tool_call.id)
            ctx.extra.pop(RESUME_USER_INPUT_KEY, None)
            setattr(ctx, ROOT_NON_PERMISSION_RESUME_ATTRIBUTE, RootNonPermissionResume(
                ctx.session.get_session_id(), ctx.inputs.tool_call.id, "task_tool",
            ))
        else:
            await super().before_tool_call(ctx)
        if not ctx.extra.get("_skip_tool"):
            ctx.extra["_cua_approval_binding"] = self._code_approvals.bind(ctx, user_input)

    async def after_tool_call(self, ctx):
        self._reset_code_binding(ctx)

    async def on_tool_exception(self, ctx):
        self._reset_code_binding(ctx)

    def _reset_code_binding(self, ctx):
        token = ctx.extra.pop("_cua_approval_binding", None)
        if token is not None:
            self._code_approvals.reset(token)

    def uninit(self, agent):
        self._code_approvals.cancel()
