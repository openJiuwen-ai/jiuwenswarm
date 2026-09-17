# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt and candidate lifecycle rail for Symphony orchestration."""

from __future__ import annotations

import re
from typing import Any, Callable

from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

_ConfigBaseProvider = dict[str, Any] | Callable[[], dict[str, Any] | None] | None
_SHORTLIST_EXTRA_KEY = "symphony_candidate_skill_ids"
_GRAPH_BUILD_TIMEOUT_EXTRA_KEY = "symphony_graph_build_timeout"
_GRAPH_TOOL_NAMES = frozenset({
    "symphony_compose_graph",
    "symphony_refresh_graph",
})
_ABILITY_MANAGER_TIMEOUT_PATTERN = re.compile(
    r"Tool '([^']+)' timed out after ([0-9]+(?:\.[0-9]+)?)s"
)

_MANUAL_GRAPH_BUILD_CONTENT = {
    "cn": (
        "技能较多时，技能图谱构建时间可能较长，本次会话内构建已超时。请前往「我的技能」>「技能图谱」，"
        "点击「增量构建」并等待构建完成；完成后请重新发送任务。为避免再次超时，本轮不会重复调用图谱构建。"
    ),
    "en": (
        "With many skills, building the Skill Graph can take a while and timed out in this session. "
        "Go to My Skills > Skill Graph, click Incremental Build, and wait for it to finish; then resend your task. "
        "To avoid another timeout, this round will not call the graph-building tools again."
    ),
}


class SymphonyOrchestrationRail(DeepAgentRail):
    """Manage Symphony guidance, candidate injection, and viewed Skills."""

    priority = 98
    SECTION_NAME = "symphony_orchestration"
    SECTION_PRIORITY = 42
    COMPOSE_TOOL_NAME = "symphony_compose_graph"

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
        self._remove_graph_tools_after_timeout(ctx)
        self._sync_orchestration_guidance(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._inject_shortlisted_candidates(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._terminate_structured_graph_build_timeout(ctx)
        self._remember_successful_skill_view(ctx)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        """Turn the outer AbilityManager timeout into the same terminal result."""
        if not self._is_graph_tool_call(ctx) or not self._is_outer_graph_timeout(ctx):
            return
        self._terminate_graph_build_timeout(ctx, self._outer_timeout_payload(ctx))

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

    def _terminate_structured_graph_build_timeout(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not self._is_graph_tool_call(ctx):
            return
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        result = inputs.tool_result
        if not isinstance(result, dict) or result.get("reason") != "graph_build_timeout":
            return
        if (
            result.get("direct_display") is True
            and result.get("followup_action") == "manual_graph_build"
        ):
            return
        self._terminate_graph_build_timeout(ctx, result)

    def _terminate_graph_build_timeout(
        self,
        ctx: AgentCallbackContext,
        result: dict[str, Any],
    ) -> None:
        payload = dict(result)
        payload.update(
            {
                "success": False,
                "reason": "graph_build_timeout",
                "timed_out": True,
                "retryable": False,
                "direct_display": True,
                "continue_after_display": False,
                "followup_action": "manual_graph_build",
                "content": self._manual_graph_build_content(),
            }
        )
        if isinstance(ctx.inputs, ToolCallInputs):
            ctx.inputs.tool_result = payload
        ctx.extra[_GRAPH_BUILD_TIMEOUT_EXTRA_KEY] = True
        ctx.request_force_finish({"output": payload["content"], "result_type": "answer"})

    def _remove_graph_tools_after_timeout(self, ctx: AgentCallbackContext) -> None:
        if not ctx.extra.get(_GRAPH_BUILD_TIMEOUT_EXTRA_KEY):
            return
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        tools = ctx.inputs.tools
        if not isinstance(tools, list):
            return
        ctx.inputs.tools = [
            tool for tool in tools if self._model_tool_name(tool) not in _GRAPH_TOOL_NAMES
        ]

    def _manual_graph_build_content(self) -> str:
        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        return _MANUAL_GRAPH_BUILD_CONTENT["en" if language == "en" else "cn"]

    @classmethod
    def _is_graph_tool_call(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        return isinstance(inputs, ToolCallInputs) and cls._tool_name(inputs) in _GRAPH_TOOL_NAMES

    @classmethod
    def _is_outer_graph_timeout(cls, ctx: AgentCallbackContext) -> bool:
        exception = ctx.exception
        if not isinstance(exception, AbilityExecutionError):
            return False
        if not isinstance(exception.__cause__, TimeoutError):
            return False
        match = _ABILITY_MANAGER_TIMEOUT_PATTERN.fullmatch(
            cls._exception_message(exception)
        )
        return match is not None and match.group(1) == cls._tool_name(ctx.inputs)

    @staticmethod
    def _exception_message(exception: AbilityExecutionError) -> str:
        tool_message = exception.tool_message
        content = getattr(tool_message, "content", None)
        return content.strip() if isinstance(content, str) else ""

    @classmethod
    def _outer_timeout_payload(cls, ctx: AgentCallbackContext) -> dict[str, Any]:
        tool_name = cls._tool_name(ctx.inputs)
        operation = "plan" if tool_name == cls.COMPOSE_TOOL_NAME else "refresh_graph"
        exception = ctx.exception
        message = cls._exception_message(exception)
        payload: dict[str, Any] = {
            "success": False,
            "reason": "graph_build_timeout",
            "timed_out": True,
            "retryable": False,
            "operation": operation,
            "detail": message,
        }
        match = _ABILITY_MANAGER_TIMEOUT_PATTERN.fullmatch(message)
        if match:
            payload["timeout_s"] = float(match.group(2))
        return payload

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
## Skill Orchestration Contract

Before executing Skills or answering, you MUST call `symphony_compose_graph`
with the original user task as `query` when ANY of these conditions is true:

1. The user explicitly requests using, selecting, combining, or orchestrating
   Skills, including requests that mention skill(s) or 技能.
2. The task requires two or more specialized capabilities or an ordered
   toolchain.
3. You have identified, inspected, selected, invoked, or recommended any
   installed Skill for the task.

Calling `skill_branch_explore` creates a mandatory orchestration follow-up. After
calling it, select only the few Skills relevant to the original user task and
call `symphony_compose_graph` before executing any Skill or returning a final
answer. Pass the selected Skills' exact identifiers or names as
`candidate_skill_ids`; never pass every Skill returned by exploration. If no
candidate can be selected confidently, still call `symphony_compose_graph`
with the original query and omit `candidate_skill_ids`.

Do not choose the execution chain yourself; the orchestration tool determines
Skill ordering and graph composition. After it returns, present its returned
`content` directly to the user. If the orchestration tool reports missing
inputs, ask for those inputs.

If the orchestration tool reports no suitable candidates, a missing capability,
or caveats that point to a Skill gap, use `search_skill` to discover external
Skills. When installing a discovered Skill is appropriate, call
`install_skill`; after a successful install, call `symphony_refresh_graph` and
then call `symphony_compose_graph` again with the original user task.

If either graph tool returns `graph_build_timeout` or `manual_graph_build`, do
not call `symphony_compose_graph` or `symphony_refresh_graph` again in the
current round. Tell the user to build the graph manually instead.

Skip skill orchestration only when none of the three trigger conditions is true
and `skill_branch_explore` was not called in the current round.
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
