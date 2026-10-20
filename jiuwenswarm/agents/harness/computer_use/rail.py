"""Materialize isolated tool owners and release them on every task exit."""

from __future__ import annotations

import asyncio
import sys
import time
from uuid import uuid4

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail, ToolCallInputs
from openjiuwen.harness.tools.cua.cua_capabilities import (
    DEFAULT_CUA_AGENT_CAPABILITY_NAMES,
    resolve_cua_capabilities,
)
from openjiuwen.harness.tools.cua.desktop_tools import CuaDesktop, build_cua_desktop_tools
from openjiuwen.harness.tools.cua.rails import CuaProgressRail, CuaRepeatFailureRail, CuaScreenshotDownscaleRail
from openjiuwen.harness.tools.cua.user_activity import build_default_user_activity_probe

from .runtime import ReplRuntime
from .tools import CuaReplTool, NativeTool
from .macos_backend import MacOSDesktop, MacOSLaunchTool


class UserInputGuard:
    """Pause at call boundaries, distinguishing the previous call's own input."""

    def __init__(self, enabled):
        self.probe = build_default_user_activity_probe() if enabled else None
        self.started = self.finished = None

    def idle(self):
        value = self.probe.seconds_since_last_input() if self.probe is not None else None
        if value is not None and self.started is not None and self.finished is not None:
            event_at = time.monotonic() - value
            if self.started - 1.5 <= event_at <= self.finished + 1.5:
                return None
        return value

    async def __call__(self):
        idle = self.idle()
        if idle is not None and idle < 3:
            deadline = time.monotonic() + 60
            while idle is not None and idle < 5:
                if time.monotonic() >= deadline:
                    raise RuntimeError("User is still using the desktop. No command was dispatched; report the takeover.")
                await asyncio.sleep(0.5)
                idle = self.idle()
        self.started, self.finished = time.monotonic(), None

    def complete(self):
        self.finished = time.monotonic()


class ComputerUseRail(AgentRail):
    priority = 90

    def __init__(self, *, repl_enabled=True, python=None, pause_on_user_input=False):
        self.repl_enabled, self.python, self.pause_on_user_input = repl_enabled, python, pause_on_user_input
        self.owner_id = "cua_" + uuid4().hex
        self.runtime = ReplRuntime(python)
        self.desktop = MacOSDesktop(python) if sys.platform == "darwin" else CuaDesktop()
        self.guard = UserInputGuard(pause_on_user_input)
        self.progress = CuaProgressRail("")
        self.repeats = CuaRepeatFailureRail("")
        self.downscale = CuaScreenshotDownscaleRail("")
        self.tools = []
        self.repl_errors = []
        self.native_ready = False
        self.session_id = None

    def fork_for_agent(self):
        return ComputerUseRail(repl_enabled=self.repl_enabled, python=self.python,
                               pause_on_user_input=self.pause_on_user_input)

    def init(self, agent):
        # AgentCard ids are stable for checkpoints; live resources need unique
        # owners or a sibling can silently replace same-named tools globally.
        agent.ability_manager.set_owner_id(self.owner_id)
        names = resolve_cua_capabilities(DEFAULT_CUA_AGENT_CAPABILITY_NAMES).allowed_tool_names
        self.tools = []
        for tool in build_cua_desktop_tools(self.desktop, names=names):
            if isinstance(self.desktop, MacOSDesktop) and tool.card.name == "launch_app":
                tool = MacOSLaunchTool(tool, self.desktop)
            self.tools.append(NativeTool(tool, self.owner_id,
                lambda name=tool.card.name: self.preflight_native(name), self.guard))
        if self.repl_enabled:
            repl = CuaReplTool(self.runtime, self.owner_id, self.guard)
            if sys.version_info < (3, 12) and not self.python:
                repl.failure = "Python 3.12+ is required; configure react.subagents.cua_agent.repl_python."
            if sys.platform not in ("darwin", "win32"):
                repl.failure = "The bundled cua_repl engine currently supports macOS and Windows. Use the original tools on Linux."
            self.tools.append(repl)
        for tool in self.tools:
            agent.ability_manager.add_ability(tool.card, tool)
        agent.register_task_resource_cleanup(self.close)

    async def preflight_native(self, name=None):
        if not self.native_ready:
            await asyncio.to_thread(lambda: self.desktop.backend.screen_size())
            self.native_ready = True
        if isinstance(self.desktop, MacOSDesktop):
            self.desktop.backend.begin_call(observation_only=name in ("get_window_state", "zoom"))

    async def before_invoke(self, ctx: AgentCallbackContext):
        session_id = ctx.session.get_session_id() if ctx.session is not None else self.owner_id
        if self.runtime.closing or self.session_id is not None and self.session_id != session_id:
            await self.runtime.close()
            if isinstance(self.desktop, MacOSDesktop):
                await asyncio.to_thread(self.desktop.close)
                self.desktop.reset()
                self.native_ready = False
            self.runtime = ReplRuntime(self.python)
            for tool in self.tools:
                if isinstance(tool, CuaReplTool):
                    tool.runtime = self.runtime
                    tool.last_failed_code, tool.same_failures = None, 0
        self.session_id = session_id
        self.repl_errors = []
        await self.progress.before_invoke(ctx)
        await self.repeats.before_invoke(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext):
        await self.repeats.before_tool_call(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext):
        self.guard.complete()
        inputs = ctx.inputs
        if isinstance(inputs, ToolCallInputs) and inputs.tool_name == "cua_repl":
            result = inputs.tool_result
            if getattr(result, "success", None) is False:
                self.repl_errors.append(str(getattr(result, "error", None) or "REPL execution failed")[:300])
            # Preserve engine PNG bytes/dimensions. Native tools retain their
            # existing downscale rail and coordinate-scale notes separately.
            return
        await self.downscale.after_tool_call(ctx)
        await self.repeats.after_tool_call(ctx)
        await self.progress.after_tool_call(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext):
        await self.progress.after_invoke(ctx)
        result = getattr(ctx.inputs, "result", None)
        if isinstance(result, dict) and self.repl_errors:
            progress = result.get("cua_result", {})
            if progress.get("status") == "completed":
                progress["status"] = "partial"
            progress["blockers"] = list(progress.get("blockers", [])) + self.repl_errors[-5:]
            progress["recommended_recovery"] = "Observe current UI before continuing; prior script actions may have executed."

    async def close(self):
        try:
            await self.runtime.close()
        finally:
            if isinstance(self.desktop, MacOSDesktop):
                try:
                    await asyncio.to_thread(self.desktop.close)
                except Exception as error:
                    from .runtime import desktop_lane
                    desktop_lane().blocker = str(error)
                    raise

    def uninit(self, agent):
        self.runtime.request_close()
        if isinstance(self.desktop, MacOSDesktop):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.desktop.close()
            else:
                loop.create_task(self.close())
        for tool in self.tools:
            agent.ability_manager.remove_ability(tool.card.name)
        self.tools = []
