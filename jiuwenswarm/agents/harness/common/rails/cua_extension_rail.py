"""Register the REPL and bind its lifetime to the existing delegated task."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from openjiuwen.core.single_agent.rail.base import AgentRail, ToolCallInputs
from openjiuwen.harness.tools.cua.rails import CuaProgressRail

from ..tools.cua_repl import CuaReplTool
from ..tools.cua_runtime import ReplRuntime


class CuaExtensionRail(AgentRail):
    priority = 90

    def __init__(self, *, pause_on_user_input=False):
        self.pause_on_user_input = pause_on_user_input
        self.owner_id = "cua_" + uuid4().hex
        self.runtime = ReplRuntime()
        self.session_id = None
        self.repl = self.progress = None

    def fork_for_agent(self):
        return CuaExtensionRail(pause_on_user_input=self.pause_on_user_input)

    def init(self, agent):
        self.progress, = agent.find_rails_by_type((CuaProgressRail,))
        self.repl = CuaReplTool(self.runtime, self.owner_id, pause_on_user_input=self.pause_on_user_input)
        agent.ability_manager.add_ability(self.repl.card, self.repl)
        agent.register_task_resource_cleanup(self.close)

    async def before_invoke(self, ctx):
        session_id = ctx.session.get_session_id() if ctx.session is not None else self.owner_id
        if self.runtime.closing or self.session_id is not None and self.session_id != session_id:
            await self.runtime.close()
            self.runtime = ReplRuntime()
            self.repl.runtime = self.runtime
            self.repl.last_failed_code, self.repl.same_failures = None, 0
        self.session_id = session_id

    async def after_tool_call(self, ctx):
        inputs = ctx.inputs
        if isinstance(inputs, ToolCallInputs) and inputs.tool_name == "cua_repl":
            result = inputs.tool_result
            if getattr(result, "success", None) is False:
                # Preserve the existing desktop progress/blocker reporting.
                # pylint: disable-next=protected-access
                self.progress._note_blocker("cua_repl", str(result.error))

    async def close(self):
        await self.runtime.close()

    def uninit(self, agent):
        self.runtime.request_close()
        try:
            asyncio.get_running_loop().create_task(self.close())
        except RuntimeError:
            pass  # Task resource cleanup owns shutdown before the loop exits.
