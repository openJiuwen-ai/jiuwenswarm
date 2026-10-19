# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt and candidate lifecycle rail for Symphony orchestration."""

from __future__ import annotations

from typing import Any, Callable

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

_ConfigBaseProvider = dict[str, Any] | Callable[[], dict[str, Any] | None] | None
_SHORTLIST_EXTRA_KEY = "symphony_candidate_skill_ids"


class SymphonyOrchestrationRail(DeepAgentRail):
    """Manage Symphony guidance, candidate injection, and viewed Skills."""

    priority = 98
    SECTION_NAME = "symphony_orchestration"
    SECTION_PRIORITY = 42
    COMPOSE_TOOL_NAME = "symphony_compose_score"

    def __init__(self, *, config_base: _ConfigBaseProvider = None) -> None:
        super().__init__()
        self._config_base = config_base
        self.system_prompt_builder = None

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._sync_orchestration_guidance(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._inject_shortlisted_candidates(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._remember_successful_skill_view(ctx)

    def _sync_orchestration_guidance(self, ctx: AgentCallbackContext) -> None:
        builder = self.system_prompt_builder
        if builder is None:
            return
        if not self._has_compose_tool(ctx) or not self._orchestration_enabled():
            builder.remove_section(self.SECTION_NAME)
            return

        language = getattr(builder, "language", "cn") or "cn"
        builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={language: self._build_orchestration_guidance()},
                priority=self.SECTION_PRIORITY,
            )
        )

    def _inject_shortlisted_candidates(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if self._tool_name(ctx.inputs) != self.COMPOSE_TOOL_NAME:
            return

        args = self._tool_args(ctx.inputs)
        if "candidate_skill_ids" in args:
            return
        shortlisted = self._shortlisted_skill_ids(ctx)
        if not shortlisted:
            return

        args["candidate_skill_ids"] = shortlisted
        ctx.inputs.tool_args = args
        if ctx.inputs.tool_call is not None:
            ctx.inputs.tool_call.arguments = args

    def _remember_successful_skill_view(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if not self._is_skill_tool(self._tool_name(ctx.inputs)):
            return
        if ctx.exception is not None:
            return
        if self._tool_result_failed(ctx.inputs.tool_result):
            return

        args = self._tool_args(ctx.inputs)
        skill_name = str(args.get("skill_name") or args.get("skillName") or "").strip()
        if not skill_name:
            return
        shortlisted = self._shortlisted_skill_ids(ctx)
        if skill_name not in shortlisted:
            shortlisted.append(skill_name)
        ctx.extra[_SHORTLIST_EXTRA_KEY] = shortlisted

    def _orchestration_enabled(self) -> bool:
        try:
            from jiuwenswarm.symphony.config import load_symphony_config

            config_base = self._resolve_config_base()
            config = (
                load_symphony_config()
                if config_base is None
                else load_symphony_config(config_base)
            )
        except Exception:
            return False
        return bool(config.enabled)

    @staticmethod
    def _build_orchestration_guidance() -> str:
        return """
## Symphony Orchestration

When the user says to use skill(s) or 技能, or when you judge that skill
capabilities, skill chaining, skill ordering, or a specialized toolchain could
help complete the task, you MUST call `symphony_compose_score` with the original
user task as `query` before answering.
When you identify, inspect, or recommend installed Skills that are relevant to
the task, you MUST pass their exact identifiers or names as
`symphony_compose_score.candidate_skill_ids`. Do not omit this field after
selecting candidate Skills. Do not choose the execution chain yourself;
Symphony owns ordering and graph composition. After it returns, present its
returned `content` directly to the user. If Symphony reports missing inputs,
ask for those inputs.

If Symphony reports no suitable candidates, a missing capability, or caveats
that point to a skill gap, use `search_skill` to discover external skills. When
installing a discovered skill is appropriate, call `install_skill`; after a
successful install, call `symphony_refresh_score` and then call
`symphony_compose_score` again with the original user task.

For clearly ordinary tasks that do not benefit from skill capabilities, continue
normally without Symphony.
"""

    @staticmethod
    def _tool_name(inputs: ToolCallInputs) -> str:
        return str(
            inputs.tool_name or getattr(inputs.tool_call, "name", "") or ""
        ).strip()

    @staticmethod
    def _is_skill_tool(name: str) -> bool:
        return str(name or "").strip().lower() == "skill_tool"

    @staticmethod
    def _tool_args(inputs: ToolCallInputs) -> dict[str, Any]:
        if isinstance(inputs.tool_args, dict):
            return dict(inputs.tool_args)
        raw_args = getattr(inputs.tool_call, "arguments", None)
        if isinstance(raw_args, dict):
            return dict(raw_args)
        return {}

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("success") is False:
            return True
        return str(result.get("status") or "").strip().lower() == "error"

    @staticmethod
    def _shortlisted_skill_ids(ctx: AgentCallbackContext) -> list[str]:
        values = ctx.extra.get(_SHORTLIST_EXTRA_KEY)
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @classmethod
    def _has_compose_tool(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return False
        return any(
            cls._model_tool_name(tool) == cls.COMPOSE_TOOL_NAME for tool in tools
        )

    @staticmethod
    def _model_tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict):
                return str(function.get("name", "") or "")
            return str(tool.get("name", "") or "")
        function = getattr(tool, "function", None)
        if isinstance(function, dict):
            return str(function.get("name", "") or "")
        function_name = getattr(function, "name", None)
        if function_name is not None:
            return str(function_name)
        name = getattr(tool, "name", None)
        if name is not None:
            return str(name)
        card = getattr(tool, "card", None)
        return str(getattr(card, "name", "") or "")

    def _resolve_config_base(self) -> dict[str, Any] | None:
        if callable(self._config_base):
            return self._config_base()
        return self._config_base


__all__ = ["SymphonyOrchestrationRail"]
