# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject role-split team cost discipline into the system prompt."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.agents.harness.team.accel.prompts import (
    cost_discipline_section,
    normalize_team_role,
)


class AccelPromptRail(DeepAgentRail):
    """Role-split cost rules for every team member.

    ResponsePromptRail already supplies thinking discipline (including the
    write-file turn). This rail adds one short leader or member cost section
    that also covers minimal messaging and batched writes.

    ``role`` should be ``leader`` or ``teammate`` (other labels normalize to
    teammate). Swarm assembly passes it from ``BuildContext.role``; the legacy
    ``build_member_rails`` path passes ``MemberInfo.role``.
    """

    priority = 4

    def __init__(self, role: str | None = None) -> None:
        super().__init__()
        self._role = normalize_team_role(role)
        self.system_prompt_builder = None

    @property
    def role(self) -> str:
        return self._role

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        del agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(
                LocalSectionName.TEAM_COST_DISCIPLINE
            )
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        del ctx
        builder = self.system_prompt_builder
        if builder is None:
            return
        builder.add_section(cost_discipline_section(self._role))
