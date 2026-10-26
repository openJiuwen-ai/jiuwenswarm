# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject team permission policy into member prompts."""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.security.narrowing import format_base_permissions_for_desc
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail


class TeamPermissionPolicyRail(DeepAgentRail):
    """Tell the leader what base permission rules teammates operate under.

    Only mounted on the leader when ``enable_permissions`` is true. The
    section describes the base permission configuration so the leader can
    make informed narrowing decisions when calling ``spawn_teammate``.
    """

    priority = 5
    SECTION_NAME = "team_permission_policy"
    SECTION_PRIORITY = 39

    def __init__(
        self,
        *,
        permissions_config: dict[str, Any],
        language: str = "cn",
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._permissions_config = permissions_config
        self._language = language

    def init(self, agent) -> None:
        """Capture the prompt builder owned by the current member."""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        """Remove the injected policy section."""
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Inject the permission policy before each model call."""
        _ = ctx
        if self.system_prompt_builder is None:
            return

        permissions_text = format_base_permissions_for_desc(
            self._permissions_config, lang=self._language,
        )
        if not permissions_text:
            return

        heading = (
            "# Teammate Permission Rules\n\n"
            if self._language != "cn"
            else "# Teammate 权限规则\n\n"
        )

        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={self._language: heading + permissions_text + "\n"},
                priority=self.SECTION_PRIORITY,
            )
        )


__all__ = ["TeamPermissionPolicyRail"]