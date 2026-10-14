"""Per-child runtime binding through existing DeepAgent lifecycle hooks."""

from __future__ import annotations

import asyncio
from typing import Any

from openjiuwen.core.single_agent.prompts.builder import PromptSection
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail

from .config import ComputerUseSettings, load_skill
from .runtime import ComputerUseRuntime
from .tools import ComputerUseTool


class ComputerUseRail(AgentRail):
    priority = 90

    def __init__(self, settings: ComputerUseSettings):
        self.settings = settings
        self.runtime: ComputerUseRuntime | None = None
        self.tools: list[ComputerUseTool] = []

    def fork_for_agent(self) -> ComputerUseRail:
        return ComputerUseRail(self.settings)

    def init(self, agent: Any) -> None:
        if self.runtime is None:
            self.runtime = ComputerUseRuntime(self.settings)
        # Logical AgentCard ids stay stable for checkpoints. Live tool owners
        # must be unique or Runner.resource_mgr silently rebinds sibling tools.
        agent.ability_manager.set_owner_id(self.runtime.id)
        self.tools = [
            ComputerUseTool(self.runtime),
            ComputerUseTool(self.runtime, reset=True),
        ]
        for tool in self.tools:
            agent.ability_manager.add_ability(tool.card, tool)
        agent.register_task_resource_cleanup(self.runtime.close)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if self.runtime is None:
            raise RuntimeError("Computer Use rail was not initialized")
        try:
            await self.runtime.open()
            for tool in self.tools:
                remote = self.runtime.tools[tool.card.name]
                tool.card.input_params = remote.inputSchema
                tool.card.description = remote.description or tool.card.description
                tool.startup_error = None
            skill = load_skill(self.settings, self.runtime.platform)
            ctx.agent.system_prompt_builder.add_section(
                PromptSection(
                    name="computer_use_skill",
                    priority=85,
                    content={"en": skill, "cn": skill},
                )
            )
        except asyncio.CancelledError:
            await asyncio.shield(self.runtime.close())
            raise
        except Exception as error:
            # Core callback errors are logged and suppressed. Disable tools
            # explicitly so a failed Skill/transport initialization cannot be
            # bypassed by the next tool call's lazy open().
            detail = str(error)
            try:
                await self.runtime.close()
            except Exception as cleanup:
                detail += "; cleanup: " + str(cleanup)
            for tool in self.tools:
                tool.startup_error = detail
            warning = (
                "Computer Use is unavailable: "
                + detail
                + ". No UI command was dispatched. Report this limitation."
            )
            ctx.agent.system_prompt_builder.add_section(
                PromptSection(
                    name="computer_use_skill",
                    priority=85,
                    content={"en": warning, "cn": warning},
                )
            )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if self.runtime is not None:
            await self.runtime.close()

    def uninit(self, agent: Any) -> None:
        if self.runtime is not None:
            self.runtime.request_close()
        for tool in self.tools:
            agent.ability_manager.remove_ability(tool.card.name)
        self.tools = []
