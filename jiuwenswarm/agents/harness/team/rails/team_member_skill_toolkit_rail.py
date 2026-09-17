# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register member-scoped skill-management tools for team mode.

Backward-incompatible rename: the constructor keyword ``refresh_links`` is now
``on_skill_library_changed``. It no longer refreshes a materialized link view —
there is none — it reports that the single physical Skill library changed.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
from jiuwenswarm.agents.harness.team.rails.team_skill_library_reload_rail import (
    reload_agent_skill_views,
)

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

logger = logging.getLogger(__name__)

# Skill tools that mutate the single physical Skill library.
SKILL_LIBRARY_MUTATING_TOOLS = frozenset({"install_skill", "uninstall_skill"})


class MemberSkillToolkitRail(DeepAgentRail):
    """Bind skill-management tools to a team member workspace."""

    priority = 95

    def __init__(
        self,
        workspace_dir: str,
        *,
        manager: Any | None = None,
        on_skill_library_changed: Callable[[dict[str, object]], Awaitable[None] | None] | None = None,
    ) -> None:
        """Bind the rail to one member workspace.

        Args:
            workspace_dir: Member workspace root owning the skill manager.
            manager: Pre-built skill manager; one is created when omitted.
            on_skill_library_changed: Optional hook invoked with the tool result
                after a successful install/uninstall, before the member's Skill
                view is reloaded.
        """
        super().__init__()
        self._workspace_dir = workspace_dir
        self._manager = manager
        self._on_skill_library_changed = on_skill_library_changed
        self._tools = None
        # Owner agent, needed to reload its Skill view after a library change.
        # The agent owns this rail, so the pair forms a reference cycle that
        # ``uninit`` breaks and the cyclic collector reclaims otherwise.
        self._agent: "DeepAgent | None" = None

    def init(self, agent: "DeepAgent") -> None:
        """Register member-scoped skill tools on the agent."""
        if self._tools is not None:
            return

        self._agent = agent
        if self._manager is None:
            self._manager = SkillManager(workspace_dir=self._workspace_dir)
        toolkit = SkillToolkit(manager=self._manager)
        tools = self._wrap_skill_tools(toolkit.get_tools())

        # ``add_ability`` qualifies the (stateful) skill-tool id with the owner
        # agent id and registers with refresh=True, so this rail's registration
        # deterministically wins over the declarative skill-toolkit tool element
        # carrying the same name.
        for tool in tools:
            agent.ability_manager.add_ability(tool.card, tool)

        self._tools = tools
        logger.info(
            "[MemberSkillToolkitRail] Registered %d skill tools for workspace=%s agent_id=%s",
            len(tools),
            self._workspace_dir,
            str(agent.card.id or agent.card.name),
        )

    def uninit(self, agent: "DeepAgent") -> None:
        """Remove member-scoped skill tools from the agent."""
        if not self._tools:
            return

        for tool in self._tools:
            agent.ability_manager.remove_ability(tool.card.name)

        logger.info(
            "[MemberSkillToolkitRail] Unregistered %d skill tools for workspace=%s",
            len(self._tools),
            self._workspace_dir,
        )
        self._tools = None
        self._agent = None

    def _wrap_skill_tools(self, tools: list[Tool]) -> list[Tool]:
        """Reload the member's Skill view after mutating skill operations.

        Installing or uninstalling writes to the one physical Skill library,
        never to a per-member copy, and an install deliberately does not extend
        any allow-list: a member whose ``skills-visibility.json`` carries an
        empty allow-list already inherits the whole library, and one with an
        explicit allow-list must be granted the new Skill on purpose. All that
        is left after a successful mutation is to make the member re-read the
        library and its visibility metadata.
        """
        wrapped: list[Tool] = []
        for tool in tools:
            if tool.card.name not in SKILL_LIBRARY_MUTATING_TOOLS:
                wrapped.append(tool)
                continue

            async def reload_after_call(_tool: Tool = tool, **kwargs):
                result = await _tool.invoke(kwargs, skip_inputs_validate=True)
                if isinstance(result, dict) and result.get("success"):
                    await self._notify_skill_library_changed(result)
                return result

            wrapped.append(LocalFunction(card=tool.card, func=reload_after_call))
        return wrapped

    async def _notify_skill_library_changed(self, result: dict[str, object]) -> None:
        """Run the change hook, then reload the member's Skill view."""
        if self._on_skill_library_changed is not None:
            try:
                hook_result = self._on_skill_library_changed(result)
                if inspect.isawaitable(hook_result):
                    await hook_result
            except Exception as exc:
                logger.warning(
                    "[MemberSkillToolkitRail] skill library change hook failed: %s",
                    exc,
                )

        if self._agent is None:
            return
        await reload_agent_skill_views(self._agent)


__all__ = ["MemberSkillToolkitRail", "SKILL_LIBRARY_MUTATING_TOOLS"]
