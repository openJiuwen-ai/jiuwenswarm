# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Inject the response/message-format section before each model call."""
from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.deep_agent.prompt_builder import _response_prompt


class ResponsePromptRail(DeepAgentRail):
    """Inject the response section as an independent prompt section."""

    priority = 5

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("response")
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return

        section = _response_prompt(self.system_prompt_builder.language or "cn")
        self.system_prompt_builder.add_section(section)
