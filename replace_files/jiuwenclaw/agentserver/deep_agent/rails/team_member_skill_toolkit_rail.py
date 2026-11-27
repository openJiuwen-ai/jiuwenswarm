# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register member-scoped skill-management tools for team mode."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.skill_manager import SkillManager
from jiuwenclaw.agentserver.tools.skill_toolkits import SkillToolkit

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

logger = logging.getLogger(__name__)


class MemberSkillToolkitRail(DeepAgentRail):
    """Bind skill-management tools to a team member workspace."""

    priority = 95

    def __init__(self, workspace_dir: str) -> None:
        super().__init__()
        self._workspace_dir = workspace_dir
        self._tools = None
        self._manager = None

    def init(self, agent: "DeepAgent") -> None:
        """Register member-scoped skill tools on the agent."""
        if self._tools is not None:
            return

        agent_id = str(agent.card.id or agent.card.name)
        self._manager = SkillManager(workspace_dir=self._workspace_dir)
        toolkit = SkillToolkit(manager=self._manager)
        tools = toolkit.get_tools()

        for tool in tools:
            tool.card.id = self._qualify_tool_id(tool.card.id, agent_id)

        for tool in tools:
            existing = agent.ability_manager.get(tool.card.name)
            if isinstance(existing, ToolCard):
                agent.ability_manager.remove(tool.card.name)

        Runner.resource_mgr.add_tool(list(tools))
        for tool in tools:
            agent.ability_manager.add(tool.card)

        self._tools = tools
        logger.info(
            "[MemberSkillToolkitRail] Registered %d skill tools for workspace=%s agent_id=%s",
            len(tools),
            self._workspace_dir,
            agent_id,
        )

    def uninit(self, agent: "DeepAgent") -> None:
        """Remove member-scoped skill tools from the agent."""
        if not self._tools:
            return

        for tool in self._tools:
            agent.ability_manager.remove(tool.card.name)
            Runner.resource_mgr.remove_tool(tool.card.id)

        logger.info(
            "[MemberSkillToolkitRail] Unregistered %d skill tools for workspace=%s",
            len(self._tools),
            self._workspace_dir,
        )
        self._tools = None
        self._manager = None

    @staticmethod
    def _qualify_tool_id(tool_id: str, agent_id: str) -> str:
        return f"{tool_id}_{agent_id}"


__all__ = ["MemberSkillToolkitRail"]
