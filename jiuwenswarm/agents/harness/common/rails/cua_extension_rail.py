"""Add REPL lifetime and a per-instance macOS adapter to the core CUA agent."""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.rail.base import AgentRail, ToolCallInputs
from openjiuwen.harness.tools.cua.desktop_tools import CuaDesktopTool
from openjiuwen.harness.tools.cua.rails import CuaProgressRail, CuaRuntimeRail

from jiuwenswarm.cua_engine.runtime import ReplRuntime, desktop_lane

from ..tools.cua_macos_backend import MacOSBackend, MacOSLaunchTool, install_macos_backend
from ..tools.cua_repl import CuaReplTool, NativeTool


class CuaExtensionRail(AgentRail):
    priority = 90

    def __init__(self, *, repl_enabled=True, pause_on_user_input=False):
        self.repl_enabled = repl_enabled
        self.pause_on_user_input = pause_on_user_input
        self.owner_id = "cua_" + uuid4().hex
        self.runtime = ReplRuntime()
        self.desktop = self.native_rail = self.progress = self.repl = None
        self.session_id = None
        self.tools = []

    def fork_for_agent(self):
        return CuaExtensionRail(repl_enabled=self.repl_enabled,
                                pause_on_user_input=self.pause_on_user_input)

    def init(self, agent):
        self.native_rail, = agent.find_rails_by_type((CuaRuntimeRail,))
        self.progress, = agent.find_rails_by_type((CuaProgressRail,))
        # Core pins CuaDesktop inside its factory. Adapt only that instance;
        # its original tools and runtime/preflight rail keep the same holder.
        # The pinned runtime rail has no public desktop accessor.
        # pylint: disable-next=protected-access
        self.desktop = self.native_rail._desktop
        if sys.platform == "darwin":
            install_macos_backend(self.desktop)
        originals = [Runner.resource_mgr.get_tool(tool_id=card.id) for card in agent.ability_manager.list()
                     if isinstance(card, ToolCard)]
        originals = [tool for tool in originals if isinstance(tool, CuaDesktopTool)]
        # Rebind stateful resources under this materialized child, including
        # children created concurrently from the same checkpoint AgentCard.
        for tool in originals:
            agent.ability_manager.remove_ability(tool.card.name)
        agent.ability_manager.set_owner_id(self.owner_id)
        for tool in originals:
            if sys.platform == "darwin" and tool.card.name == "launch_app":
                tool = MacOSLaunchTool(tool, self.desktop)
            self.tools.append(NativeTool(tool, self.owner_id,
                lambda name=tool.card.name: self.preflight_native(name)))
        if self.repl_enabled:
            self.repl = CuaReplTool(self.runtime, self.owner_id, pause_on_user_input=self.pause_on_user_input)
            self.tools.append(self.repl)
        for tool in self.tools:
            agent.ability_manager.add_ability(tool.card, tool)
        agent.register_task_resource_cleanup(self.close)

    async def preflight_native(self, name):
        if sys.platform == "darwin":
            self.desktop.backend.begin_call(observation_only=name in ("get_window_state", "zoom"))

    async def before_invoke(self, ctx):
        session_id = ctx.session.get_session_id() if ctx.session is not None else self.owner_id
        if self.runtime.closing or self.session_id is not None and self.session_id != session_id:
            await self.close()
            self.runtime = ReplRuntime()
            if self.repl is not None:
                self.repl.runtime = self.runtime
                self.repl.last_failed_code, self.repl.same_failures = None, 0
        self.session_id = session_id

    async def after_tool_call(self, ctx):
        inputs = ctx.inputs
        if isinstance(inputs, ToolCallInputs) and inputs.tool_name == "cua_repl":
            result = inputs.tool_result
            if getattr(result, "success", None) is False:
                # Core's progress hook is private; keep its blocker semantics.
                # pylint: disable-next=protected-access
                self.progress._note_blocker("cua_repl", str(result.error))

    async def close(self):
        try:
            await self.runtime.close()
        finally:
            # Inspect without lazily creating an unused native backend.
            # pylint: disable-next=protected-access
            backend = self.desktop._backend if self.desktop is not None else None
            if isinstance(backend, MacOSBackend):
                try:
                    await asyncio.to_thread(backend.close)
                except Exception as error:
                    desktop_lane().blocker = str(error)
                    raise
                self.desktop._backend = None  # pylint: disable=protected-access
                self.desktop.zoom_origins.clear()
                self.native_rail._verified = False  # pylint: disable=protected-access

    def uninit(self, agent):
        self.runtime.request_close()
        try:
            asyncio.get_running_loop().create_task(self.close())
        except RuntimeError:
            # Synchronous teardown must not create a backend or event loop.
            # pylint: disable-next=protected-access
            if self.desktop is not None and isinstance(self.desktop._backend, MacOSBackend):
                self.desktop._backend.close()  # pylint: disable=protected-access
