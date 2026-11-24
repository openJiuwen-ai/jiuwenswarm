# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run a node of a returned Symphony plan through the SDK's synchronous TaskTool."""

from __future__ import annotations

import asyncio
import json
import logging
from asyncio import timeout
from collections.abc import Mapping
from copy import copy
from pathlib import Path
from typing import Any

import anyio
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolExposure
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.resources import (
    AgentTemplateCard,
    LoadRecord,
    aload_agent_template_package,
    read_agent_template_card,
)
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.subagent.task_tool import TaskTool, resolve_subagent_task_timeout_s

from jiuwenswarm.agents.harness.agent_observability import attach_subagent_observability
from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority
from jiuwenswarm.server.runtime.extension_package_manager import resolve_package_avatar_thumbnail

from .expert_task_plan import ExpertTask, TASK_PLAN_PROVIDER


_LOGGER = logging.getLogger(__name__)
_GENERAL_PURPOSE = "general-purpose"
_NODE_CLEANUP_GRACE_S = 5.0
SUBMIT_OUTPUTS_TOOL = "symphony_submit_outputs"
_NODE_ASSIGNMENT_PROMPT = {
    "en": (
        "You are executing a single Symphony task. Follow the role and task fields of the assignment. "
        "Use inputs (upstream outputs), external_inputs (existing task materials), and user_request as reference data. "
        "Read file references with your workspace tools and report any material gaps. "
        "The parent agent handles orchestration, the other tasks, and the reply to the user. "
        "Before submitting, check the deliverables against each acceptance criterion in task. "
        "For a plain task description, check the requirements stated in the text. "
        f"Call {SUBMIT_OUTPUTS_TOOL} with an outputs object keyed exactly by the names in the assignment's "
        "outputs array. For a file deliverable, create the file in the workspace, check that it exists, "
        "and submit its path as the value."
    ),
    "cn": (
        "你负责执行 Symphony 的一个任务节点。按照分派内容中的 role 和 task 完成本节点。"
        "inputs（前置产物）、external_inputs（现有任务资料）和 user_request 是本节点的参考数据。"
        "用工作区工具读取引用的文件，注明实际存在的资料缺口。编排、其他节点和用户交付由主智能体负责。"
        "提交前按 task 中的验收标准逐条核对产物。若 task 是普通段落，按正文中的要求核对。"
        f"调用 {SUBMIT_OUTPUTS_TOOL} 时，提交的 outputs 对象必须逐字使用任务分派中 outputs 数组的全部名称作为键。"
        "产物为文件时，在工作区生成文件，确认文件存在后提交其路径。"
    ),
}


def general_purpose_recipe(agent: DeepAgent) -> SubAgentConfig | None:
    return next(
        (
            spec for spec in agent.deep_config.subagents or ()
            if isinstance(spec, SubAgentConfig) and spec.agent_card.name == _GENERAL_PURPOSE
        ),
        None,
    )


async def _installed_template(expert_id: str) -> tuple[Path, AgentTemplateSpec, AgentTemplateCard]:
    from jiuwenswarm.agents.harness.team.experts import JiuwenExpertCatalog

    matches: list[tuple[Path, AgentTemplateCard]] = []
    for package in await JiuwenExpertCatalog().list_expert_packages():
        manifest = package.path / "manifest.json"
        try:
            card = await read_agent_template_card(manifest)
        except ValueError:
            continue
        if card.name == expert_id:
            matches.append((manifest, card))
    if not matches:
        raise ValueError(f"Expert {expert_id!r} is not installed or its manifest is unavailable")
    if len(matches) > 1:
        raise ValueError(f"Expert {expert_id!r} matches multiple installed templates")
    manifest, card = matches[0]
    spec = await aload_agent_template_package(manifest)
    if spec.agent_card.name != expert_id:
        raise ValueError(f"Installed expert {expert_id!r} changed before dispatch")
    if spec.runtime is not None:
        raise ValueError(f"Expert {expert_id!r} requires an external runtime")
    return manifest.parent, spec, card


def _copy_skill_rail(rail: SkillUseRail) -> SkillUseRail:
    # Loading a template changes the child skill cache and its tool ownership.
    return SkillUseRail(
        skills_dir=copy(rail.skills_dir),
        skill_mode=rail.skill_mode,
        list_skill_model=rail.list_skill_model,
        enable_cache=rail.enable_cache,
        include_tools=rail.include_tools,
        enabled_skills=None if rail.enabled_skills is None else list(rail.enabled_skills),
        disabled_skills=list(rail.disabled_skills),
        evolution_store=rail.evolution_store,
        multimodal_skill_mode=rail.multimodal_skill_mode,
        max_skills=rail.max_skills,
        max_total_chars=rail.max_total_chars,
    )


class _NodeCompletionRail(DeepAgentRail):
    def __init__(self, task: ExpertTask) -> None:
        super().__init__()
        self._task = task
        self.answered = False
        card = ToolCard(
            name=SUBMIT_OUTPUTS_TOOL,
            description="Submit every declared output of the current Symphony task and finish the task.",
            input_params={
                "type": "object",
                "properties": {
                    "outputs": {
                        "type": "object",
                        "description": "Values keyed by the output names in the assignment.",
                        "additionalProperties": True,
                    },
                },
                "required": ["outputs"],
                "additionalProperties": False,
            },
        )
        self.tool = LocalFunction(card, self._submit)

    def _submit(self, outputs: dict[str, Any]) -> ToolOutput:
        try:
            validated = self._task.validate_outputs(outputs)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        return ToolOutput(success=True, data={"content": json.dumps({"outputs": validated}, ensure_ascii=False)})

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or inputs.tool_name != SUBMIT_OUTPUTS_TOOL:
            return
        result = inputs.tool_result
        if isinstance(result, ToolOutput) and result.success:
            ctx.request_force_finish({"output": result.data["content"], "result_type": "answer"})

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        result = getattr(ctx.inputs, "result", None)
        self.answered = isinstance(result, Mapping) and result.get("result_type") == "answer"


class _ExpertNodeTask(TaskTool):
    """Supply a fresh child to TaskTool while retaining its session and stream lifecycle."""

    def __init__(self, tool: TaskTool, task: ExpertTask, recipe: SubAgentConfig) -> None:
        super().__init__(tool.card, tool.parent_agent, tool.language, allowed_subagent_types={_GENERAL_PURPOSE})
        self.display_task_description = task.task
        self.display_name_i18n: dict[str, str] | None = None
        self.display_avatar = ""
        self._task = task
        self._recipe = recipe
        self._completion = _NodeCompletionRail(task)
        self._child: DeepAgent | None = None
        self._template: tuple[Path, AgentTemplateSpec] | None = None
        self._template_load: asyncio.Task[LoadRecord] | None = None
        self.outputs: dict[str, Any] = {}

    async def run(self, query: str, session: Session) -> dict[str, Any]:
        expert_id = self._task.expert_id
        if expert_id is not None:
            directory, spec, card = await _installed_template(expert_id)
            self._template = (directory, spec)
            self.display_name_i18n = {
                "zh": card.display_label("zh") or expert_id,
                "en": card.display_label("en") or expert_id,
            }
            if card.avatar:
                self.display_avatar = await asyncio.to_thread(
                    resolve_package_avatar_thumbnail, directory, card.avatar,
                )
        try:
            await self.invoke({"subagent_type": _GENERAL_PURPOSE, "task_description": query}, session=session)
            return self.outputs
        finally:
            if self._child is not None:
                with anyio.CancelScope(shield=True):
                    await self._release_child(self._child)

    def _create_subagent(
        self,
        normalized_type: str,
        sub_session_id: str,
        browser_capabilities: list[str] | None,
        *,
        parent_session: Session,
        browser_query: Any,
    ) -> DeepAgent:
        # A distinct card also namespaces the stateful tools of concurrent nodes.
        from .orchestration_rail import SymphonyOrchestrationRail

        recipe = self._recipe
        parent = self.parent_agent.deep_config
        card = recipe.agent_card.model_copy(update={"id": sub_session_id, "name": self._task.role})
        rails = []
        for rail in recipe.rails or ():
            if isinstance(rail, SymphonyOrchestrationRail):
                continue
            if isinstance(rail, SkillUseRail):
                rails.append(_copy_skill_rail(rail))
                continue
            fork = getattr(rail, "fork_for_agent", None)
            rails.append(fork() if callable(fork) else copy(rail))
        sys_operation = recipe.sys_operation or parent.sys_operation
        self._child = create_deep_agent(
            model=parent.model,
            card=card,
            system_prompt=recipe.system_prompt,
            tools=[
                self._completion.tool,
                *(tool for tool in recipe.tools if getattr(tool, "card", tool).name != "symphony_compose_graph"),
            ],
            mcps=list(recipe.mcps),
            rails=[*rails, self._completion],
            workspace=recipe.workspace or parent.workspace,
            sys_operation=sys_operation,
            enable_sys_operation=sys_operation is not None,
            language=recipe.language or parent.language,
            prompt_mode=recipe.prompt_mode or parent.prompt_mode,
            skills=recipe.skills,
            backend=recipe.backend or parent.backend,
            max_iterations=recipe.max_iterations or parent.max_iterations,
            enable_task_loop=recipe.enable_task_loop,
            enable_plan_mode=recipe.enable_plan_mode,
            parallel_tool_calls=recipe.parallel_tool_calls,
            restrict_to_work_dir=recipe.restrict_to_work_dir or parent.restrict_to_work_dir,
            enable_read_image_multimodal=(
                recipe.enable_read_image_multimodal
                if recipe.enable_read_image_multimodal is not None else parent.enable_read_image_multimodal
            ),
            kv_cache_affinity_config=parent.kv_cache_affinity_config,
            invocation_log_dir=recipe.invocation_log_dir or parent.invocation_log_dir,
        )
        attach_subagent_observability(self._child)
        # Use the pinned Core's workspace binding for a child built from a custom recipe.
        return self.parent_agent._bind_inherited_artifact_root(self._child)  # pylint: disable=protected-access

    async def _invoke_subagent(
        self,
        subagent: DeepAgent,
        subagent_inputs: dict[str, Any],
        *,
        parent_session_id: str,
        session: Session | None = None,
    ) -> dict[str, Any]:
        await subagent.ensure_initialized()
        if self._template is not None:
            directory, spec = self._template
            context = BuildContext(
                language=subagent.deep_config.language,
                workspace=subagent.deep_config.workspace,
                member_card_id=subagent.card.id,
                extras={"source_root": str(directory)},
            )
            # The extension loader rolls back failed loads. Let it settle on cancellation
            # so cleanup can unload a successful, partially awaited load as well.
            self._template_load = asyncio.create_task(subagent.load_agent_template_spec(spec, context=context))
            await asyncio.shield(self._template_load)
        subagent.system_prompt_builder.add_section(PromptSection(
            name="symphony_task_assignment",
            content=_NODE_ASSIGNMENT_PROMPT,
            priority=SystemPromptPriority.TASK_EXECUTION,
        ))
        result = await super()._invoke_subagent(
            subagent, subagent_inputs, parent_session_id=parent_session_id, session=session,
        )
        if not self._completion.answered:
            raise ValueError("Subagent ended without a final answer")
        self.outputs = self._task.read_outputs(result.get("output"))
        return result

    async def _release_child(self, child: DeepAgent) -> None:
        if self._template_load is not None:
            try:
                record = await self._template_load
            except Exception:
                pass  # The loader already rolled back the unsuccessful load.
            else:
                try:
                    await child.unload_extension(record)
                except Exception:
                    _LOGGER.exception("Unable to unload Symphony node template")
        for rail in reversed(child.configured_rails()):
            if child.is_registered_rail(rail):
                try:
                    await child.unregister_rail(rail)
                except Exception:
                    _LOGGER.exception("Unable to unregister Symphony node rail %s", type(rail).__name__)
        child.ability_manager.teardown_tools()


class SymphonyTaskTool(TaskTool):
    """Extend the root task_tool with graph-node addressing."""

    accepts_tool_callback_context = True

    def __init__(self, tool: TaskTool, *, plan_only: bool) -> None:
        card = tool.card.model_copy(deep=True)
        plan_parameters = {
            "plan_id": {"type": "string", "description": "The graph.id of the current ready expert plan."},
            "node_id": {"type": "string", "description": "The key of one node in graph.nodes."},
        }
        self.plan_only = plan_only
        if plan_only:
            card.input_params["properties"] = plan_parameters
            card.input_params["required"] = list(plan_parameters)
        else:
            card.input_params["properties"].update(plan_parameters)
            # Providers can omit either pair. Validate the selected form at invocation.
            card.input_params.pop("required", None)
        card.exposure = ToolExposure.DIRECT
        card.set_exposure_declared(True)
        super().__init__(card, tool.parent_agent, tool.language, tool._allowed_subagent_types)
        self.describe(tool.card.description)

    def describe(self, ordinary_description: str) -> None:
        self.card.description = (
            "Run one node of a ready Symphony expert plan with only plan_id and node_id. "
            "For an execution request, call once per runnable expert or inline role. Send independent calls "
            "together. Dispatch a dependent node after all its predecessors return completed. "
            "Use the returned outputs for integration."
        )
        if not self.plan_only:
            self.card.description += (
                " For ordinary delegation use subagent_type and task_description.\n\n" + ordinary_description
            )

    async def invoke(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        if not self._is_plan_call(inputs):
            return await super().invoke(inputs, **kwargs)

        plan_id, node_id = inputs.get("plan_id"), inputs.get("node_id")
        valid_ids = all(isinstance(value, str) and value for value in (plan_id, node_id))
        if set(inputs) != {"plan_id", "node_id"} or not valid_ids:
            return self._result(plan_id, node_id, {"status": "rejected", "reason": "Pass only plan_id and node_id"})
        context = kwargs.get("_tool_callback_context")
        provider = getattr(context, TASK_PLAN_PROVIDER, None)
        plan = provider(context, plan_id) if callable(provider) else None
        session = kwargs.get("session")
        if plan is None or session is None or session is not context.session:
            return self._result(
                plan_id, node_id, {"status": "rejected", "reason": "No matching ready expert plan in this turn"},
            )
        if self.parent_agent.load_state(session).plan_mode.mode == "plan":
            return self._result(plan_id, node_id, {"status": "rejected", "reason": "Plan mode only allows planning"})

        status = plan.claim(node_id)
        if status:
            return self._result(plan_id, node_id, status)
        node = plan.nodes[node_id]
        try:
            recipe = general_purpose_recipe(self.parent_agent)
            if recipe is None:
                raise ValueError("No general-purpose subagent is configured")
            runner = _ExpertNodeTask(self, node, recipe)
            async with timeout(resolve_subagent_task_timeout_s()):
                outputs = await runner.run(plan.query_for(node_id), session)
            status = {"status": "completed", "outputs": outputs}
        except asyncio.CancelledError:
            plan.finish(node_id, {"status": "cancelled", "reason": "Task stopped or timed out"})
            raise
        except TimeoutError:
            status = {"status": "failed", "reason": "Subagent timed out"}
        except Exception as exc:
            status = {"status": "failed", "reason": str(exc) or type(exc).__name__}
        plan.finish(node_id, status)
        return self._result(plan_id, node_id, status)

    @staticmethod
    def _is_plan_call(inputs: Any) -> bool:
        return isinstance(inputs, dict) and ("plan_id" in inputs or "node_id" in inputs)

    def get_call_timeout(self, inputs: dict[str, Any], *, default_timeout: float) -> float:
        if self._is_plan_call(inputs):
            return resolve_subagent_task_timeout_s() + _NODE_CLEANUP_GRACE_S
        return super().get_call_timeout(inputs, default_timeout=default_timeout)

    @staticmethod
    def _result(plan_id: object, node_id: object, status: dict[str, Any]) -> ToolOutput:
        return ToolOutput(
            success=status["status"] == "completed",
            data={"plan_id": plan_id, "node_id": node_id, **status},
            error=status.get("reason"),
        )

    def render_for_llm(self, output: ToolOutput) -> str:
        if isinstance(output.data, dict) and "plan_id" in output.data:
            return json.dumps(output.data, ensure_ascii=False)
        return super().render_for_llm(output)
