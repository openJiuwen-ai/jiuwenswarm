"""Desktop-local expert discovery and task-scoped Symphony execution binding."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.harness.extension_binder import apply_extension_hot, unapply_extension_hot
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.resources import ExtensionParts, load_agent_template_package
from openjiuwen.harness.resources.extension_resolver import resolve_agent_template_parts
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.subagent_lifecycle import cleanup_subagent_task_resources
from openjiuwen.symphony import ExpertCandidate, plan_experts

from jiuwenswarm.common.utils import get_agent_experts_dir, get_expert_cache_dir
from jiuwenswarm.server.runtime.expert.expert_store import (
    CachedExpertPackageSource,
    LocalDirExpertPackageSource,
)
from jiuwenswarm.symphony.expert_skills import DynamicExpertSkillRail, snapshot_public_skills


logger = logging.getLogger(__name__)
PLAN_TOOL_NAME = "symphony_plan_experts"
_EXPERT_TYPE_PREFIX = "symphony_expert_"
_MAX_PARALLEL = 3
_PROMPT_SECTION = "symphony_expert_turn"
_SKILL_GRAPH_TOOLS = frozenset({
    "symphony_compose_score", "symphony_refresh_score",
    "symphony_compose_graph", "symphony_refresh_graph",
})
_PARENT_SCOPED_TOOLS = frozenset({
    "task_tool", "sessions_spawn", "skill_tool", "list_skill", "skill_index", "skill_index_build",
})


def _warn(message: str, *args: Any) -> None:
    try:
        logger.warning(message, *args)
    except OSError:
        pass


@dataclass(frozen=True)
class LocalExpert:
    id: str
    name: str
    description: str
    template: Any
    package_dir: Path | None = None
    source: str = "local"
    avatar: str = ""


async def scan_local_experts() -> dict[str, LocalExpert]:
    """Find installed standalone experts without contacting the package repository."""
    local_root = get_agent_experts_dir()
    cache_root = get_expert_cache_dir()
    sources = (
        ("local", local_root, LocalDirExpertPackageSource(experts_dir=local_root)),
        ("cache", cache_root, CachedExpertPackageSource()),
    )
    result: dict[str, LocalExpert] = {}
    for source_name, source_root, source in sources:
        try:
            root = source_root.resolve()
            summaries = await source.list()
        except OSError as exc:
            _warn("[symphony.experts] skip %s directory: %s", source_name, exc)
            continue
        for summary in summaries:
            if not summary.available or summary.type != "agent" or summary.id in result:
                continue
            try:
                package_dir = (root / summary.id).resolve()
                if not package_dir.is_relative_to(root):
                    continue
                template = load_agent_template_package(package_dir / "manifest.json")
            except Exception as exc:  # noqa: BLE001 - one damaged package must not hide the rest
                _warn("[symphony.experts] skip %s (%s): %s", summary.id, source_name, exc)
                continue
            raw_avatar = summary.avatar_url or summary.metadata.get("avatar")
            result[summary.id] = LocalExpert(
                id=summary.id,
                name=summary.name,
                description=summary.description,
                template=template,
                package_dir=package_dir,
                source=source_name,
                avatar=raw_avatar.strip() if isinstance(raw_avatar, str) else "",
            )
    return result


def _subagent_type(expert_id: str) -> str:
    return _EXPERT_TYPE_PREFIX + hashlib.sha256(expert_id.encode()).hexdigest()[:12]


def _subagent_config(agent: Any, expert: LocalExpert, model: Any) -> SubAgentConfig:
    template = expert.template
    config = agent.deep_config
    language = config.language or "cn"
    # Keep the model-facing subagent_type stable, but isolate runtime resources
    # across sessions and successive plans for the same expert.
    child_id = f"{agent.card.id}.{_subagent_type(expert.id)}.{uuid4().hex}"
    ctx = BuildContext(
        language=language,
        workspace=config.workspace,
        member_card_id=child_id,
        extras={
            "source_root": str(expert.package_dir or Path(".")),
            "_parent_model": model,
        },
    )
    parts = resolve_agent_template_parts(template, ctx)
    prompt_parts: list[str] = []
    for resolved in sorted(parts.prompt_sections, key=lambda part: (part.section.priority, part.section.name)):
        text = resolved.section.render(language)
        if text:
            prompt_parts.append(str(text))
    prompt_parts.append("你只负责当前专家节点任务。请返回结论、依据和产物路径；不得再次编排专家或委派其他子代理。")

    tools = {}
    # Inherit general tools as references, like fixed-team members. Registering
    # a parent's concrete Tool again would mutate its card's owner in Core.
    inherited = [getattr(tool, "card", tool) for tool in config.tools or []]
    for tool in [*inherited, *parts.tools]:
        name = str(getattr(getattr(tool, "card", tool), "name", "")).strip()
        if name and name.lower() not in _PARENT_SCOPED_TOOLS and not name.lower().startswith("symphony_"):
            tools[name] = tool.model_copy(deep=True) if isinstance(tool, ToolCard) else tool
    mcps = {}
    for mcp in [*(config.mcps or []), *parts.mcps]:
        name = str(getattr(mcp, "server_name", "") or getattr(mcp, "server_id", "")).strip()
        if name:
            mcps[name] = mcp
    return SubAgentConfig(
        agent_card=AgentCard(
            id=child_id,
            name=_subagent_type(expert.id),
            description=expert.description,
        ),
        system_prompt="\n\n".join(prompt_parts),
        tools=list(tools.values()),
        mcps=list(mcps.values()),
        model=model,
        rails=[
            *(rail for rail in parts.rails if not isinstance(rail, SkillUseRail)),
            DynamicExpertSkillRail(
                public_skills=snapshot_public_skills(agent),
                expert_skills=parts.skills,
                runtime_owner_id=child_id,
                runtime_sysop_id=f"{_subagent_type(expert.id)}_{child_id}",
                runtime_mcp_ids=[mcp.server_id for mcp in mcps.values()],
            ),
        ],
        # The rail binds resolved skills after initialization. Merely putting
        # paths in SubAgentConfig.skills does not mount them in Core's factory.
        skills=None,
        factory_kwargs={"tool_owner_id": child_id},
        workspace=None,
        language=config.language,
        # task_tool invokes each expert for one planned node with an isolated
        # conversation_id; Core's outer task loop requires a Session object.
        enable_task_loop=False,
        max_iterations=config.max_iterations,
        restrict_to_work_dir=config.restrict_to_work_dir,
    )


class DynamicExpertRound:
    """One request's bound experts and dependency-aware task_tool state."""

    def __init__(self, agent: Any, query: str, model: Any) -> None:
        self.agent = agent
        self.query = query
        self.model = model
        self.plan: dict[str, Any] | None = None
        self.response: dict[str, Any] | None = None
        self._refs: list[Any] = []
        self._skill_rails: list[DynamicExpertSkillRail] = []
        self._child_ids: set[str] = set()
        self._planning_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_MAX_PARALLEL)
        self._bindings: dict[str, str] = {}
        self._predecessors: dict[str, set[str]] = {}
        self._states: dict[str, str] = {}
        self._completed: dict[str, str] = {}
        self._inflight: dict[str, str] = {}
        self._interrupted: dict[str, str] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def attempted(self) -> bool:
        return self.response is not None

    async def compose(self, query: str) -> dict[str, Any]:
        async with self._planning_lock:
            if self._closed:
                return {"success": False, "status": "error", "reason": "round_closed"}
            if self.response is not None:
                return self.response
            try:
                experts = await scan_local_experts()
                configs: dict[str, SubAgentConfig] = {}
                for expert_id, expert in experts.items():
                    try:
                        configs[expert_id] = _subagent_config(self.agent, expert, self.model)
                    except Exception as exc:  # noqa: BLE001 - skip only this unusable package
                        _warn("[symphony.experts] invalid local expert %s: %s", expert_id, exc)
                available = [expert for expert_id, expert in experts.items() if expert_id in configs]
                plan = await plan_experts(
                    query.strip() or self.query,
                    [ExpertCandidate(e.id, e.name, e.description) for e in available],
                    self.model,
                )
                if plan["status"] == "no_plan":
                    self.response = {"success": True, **plan}
                    return self.response
                if plan["status"] != "ready":
                    raise ValueError(f"invalid expert plan status: {plan['status']}")

                graph = plan["graph"]
                nodes = graph["nodes"]
                bind_task = asyncio.create_task(apply_extension_hot(
                    self.agent,
                    ExtensionParts(subagents=[configs[expert_id] for expert_id in nodes]),
                ))
                try:
                    refs = await asyncio.shield(bind_task)
                except asyncio.CancelledError:
                    # A cancelled caller must not abandon a partially bound
                    # batch. Let Core finish its atomic bind, then undo it.
                    try:
                        refs = await bind_task
                        await unapply_extension_hot(self.agent, refs)
                    except Exception as exc:  # noqa: BLE001 - retain cancellation
                        _warn("[symphony.experts] cancelled bind cleanup failed: %s", exc)
                    raise
                if self.agent.ability_manager.get("task_tool") is None:
                    await unapply_extension_hot(self.agent, refs)
                    raise RuntimeError("当前代理无法使用 task_tool")
                self._refs = refs
                self._skill_rails = [
                    rail for expert_id in nodes for rail in getattr(configs[expert_id], "rails", []) or []
                    if isinstance(rail, DynamicExpertSkillRail)
                ]
                self._child_ids = {
                    config.agent_card.id for expert_id in nodes
                    if isinstance(config := configs[expert_id], SubAgentConfig)
                }
                self.plan = graph
                self._bindings = {expert_id: _subagent_type(expert_id) for expert_id in nodes}
                self._states = {expert_id: "pending" for expert_id in nodes}
                self._predecessors = {expert_id: set() for expert_id in nodes}
                for edge in graph["edges"]:
                    self._predecessors[edge["target"]].add(edge["source"])
                self.response = {
                    "success": True,
                    "status": "ready",
                    "planned_graph": {"graph": graph},
                    "bindings": dict(self._bindings),
                    "expert_assets": {
                        expert_id: {
                            "source": experts[expert_id].source,
                            **({"avatar": experts[expert_id].avatar} if experts[expert_id].avatar else {}),
                        }
                        for expert_id in nodes
                    },
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep ordinary chat available
                _warn("[symphony.experts] planning or binding failed: %s", exc)
                self.response = {
                    "success": False,
                    "status": "error",
                    "reason": "expert_planning_failed",
                    "detail": str(exc),
                }
            return self.response

    def _task_context(self, expert_id: str, model_task: str) -> str:
        assert self.plan is not None
        node = self.plan["nodes"][expert_id]
        parts = [f"用户任务：{self.query}", f"你的职责：{node['task']}"]
        if model_task.strip() and model_task.strip() != node["task"]:
            parts.append(f"主理人补充：{model_task.strip()}")
        upstream = [
            f"{self.plan['nodes'][key]['label']} 的结果：\n{self._completed[key]}"
            for key in sorted(self._predecessors[expert_id])
        ]
        if upstream:
            parts.append("前置专家结果：\n" + "\n\n".join(upstream))
        return "\n\n".join(parts)

    async def reserve(
        self, subagent_type: str, model_task: str, call_id: str, *, resume: bool = False,
    ) -> tuple[str | None, str]:
        await self._semaphore.acquire()
        reserved = False
        try:
            async with self._state_lock:
                expert_id = next((key for key, value in self._bindings.items() if value == subagent_type), None)
                can_resume = resume and self._interrupted.get(call_id) == expert_id
                reason = ""
                if self._closed or self.plan is None:
                    reason = "本轮专家计划已结束"
                elif expert_id is None:
                    reason = "该专家不在本轮计划中"
                elif resume and not can_resume:
                    reason = "该专家节点没有可恢复的中断调用"
                elif self._states[expert_id] != "pending" and not can_resume:
                    reason = "该专家节点已调用，不可重复执行"
                elif any(self._states[key] == "failed" for key in self._predecessors[expert_id]):
                    reason = "前置专家执行失败，不能执行该节点"
                elif not self._predecessors[expert_id] <= self._completed.keys():
                    reason = "前置专家尚未完成，请按计划依赖顺序调用"
                if reason:
                    return None, reason
                assert expert_id is not None
                self._states[expert_id] = "running"
                self._inflight[call_id] = expert_id
                self._interrupted.pop(call_id, None)
                reserved = True
                return expert_id, self._task_context(expert_id, model_task)
        finally:
            if not reserved:
                self._semaphore.release()

    async def suspend(self, call_id: str) -> None:
        async with self._state_lock:
            expert_id = self._inflight.pop(call_id, None)
            if expert_id is not None:
                self._states[expert_id] = "interrupted"
                self._interrupted[call_id] = expert_id
                self._semaphore.release()

    async def finish(self, call_id: str, *, success: bool, output: str = "") -> None:
        async with self._state_lock:
            expert_id = self._inflight.pop(call_id, None)
            if expert_id is None:
                return
            self._states[expert_id] = "completed" if success else "failed"
            if success:
                self._completed[expert_id] = output
            self._semaphore.release()

    def guidance(self) -> str:
        if self.plan is None:
            if self.response is None:
                return (
                    "仅当用户明确要求调用专家处理当前任务或组织专家协作时，调用 symphony_plan_experts。"
                    "仅提及专家或设定专家角色不代表要求调用专家。"
                    "未明确要求时，沿用常规处理流程。已召唤固定专家团时沿用当前团队流程。"
                )
            return "本轮没有可执行的专家计划。请沿用常规处理流程继续完成用户任务，不要声称专家已执行。"

        lines = [
            "按 planned_graph 依赖顺序调用真实 task_tool，完成本轮专家委派；"
            "可在同一次模型输出中并行调用没有依赖的节点。工具会阻止越序和重复调用。"
            "复核已完成结果并统一答复；未执行或失败的任务不得写成已完成。",
        ]
        for expert_id, node in self.plan["nodes"].items():
            deps = ",".join(sorted(self._predecessors[expert_id])) or "无"
            lines.append(
                f"{expert_id}: subagent_type={self._bindings[expert_id]}，任务={node['task']}，"
                f"依赖={deps}，状态={self._states[expert_id]}"
            )
        if all(state == "completed" for state in self._states.values()):
            lines.append("所有专家节点已完成。现在复核并汇总，不要再次委派。")
        elif any(state == "failed" for state in self._states.values()):
            lines.append("已有专家失败。仅继续执行不依赖失败节点的任务，并在最终答复中说明未执行部分。")
        return "\n".join(lines)

    async def close(self) -> None:
        # Own the whole close operation, not only each skill unload. A second
        # cancellation must not abandon the remaining children or parent refs.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
            self._close_task.result()
            raise

    async def _close(self) -> None:
        async with self._planning_lock:
            async with self._state_lock:
                if self._closed:
                    return
                self._closed = True
                refs, self._refs = self._refs, []
        # TaskTool deliberately retains interrupted children. The pinned Core
        # has no public drain API; remove only this round's unique child IDs.
        try:
            card = self.agent.ability_manager.get("task_tool")
            task_tool = Runner.resource_mgr.get_tool(card.id) if isinstance(card, ToolCard) else None
            pending = getattr(task_tool, "_pending_subagents", {})
            for session_id, (child, _affinity) in list(pending.items()):
                if child.card.id in self._child_ids:
                    pending.pop(session_id, None)
                    await cleanup_subagent_task_resources(child)
        except Exception as exc:  # noqa: BLE001 - still close rails and parent bindings
            _warn("[symphony.experts] failed to drain interrupted experts: %s", exc)
        for rail in self._skill_rails:
            try:
                await rail.close()
            except Exception as exc:  # noqa: BLE001 - continue cleaning other experts
                _warn("[symphony.experts] failed to unload expert skills: %s", exc)
        if refs:
            try:
                await unapply_extension_hot(self.agent, refs)
            except Exception as exc:  # noqa: BLE001 - do not mask the chat outcome
                _warn("[symphony.experts] failed to unbind experts: %s", exc)


class ExpertPlanTool(Tool):
    """Real model-callable planning tool, scoped to an active desktop request."""

    def __init__(self, round_provider: Callable[[str], DynamicExpertRound | None]) -> None:
        super().__init__(ToolCard(
            id=PLAN_TOOL_NAME,
            name=PLAN_TOOL_NAME,
            description=(
                "Plan delegation to local expert agents only when the user explicitly asks to invoke "
                "expert agents or organize expert collaboration for the current task. "
                "Merely mentioning experts or assigning an expert persona is not an invocation request. "
                "Returns a planned_graph and task_tool subagent_type names, or no_plan if no usable "
                "expert plan is available. On no_plan, continue the normal task workflow. "
                "Do not call for an already summoned fixed expert team."
            ),
            input_params={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The user's current task."}},
                "required": ["query"],
            },
        ))
        self._round_provider = round_provider

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        session = kwargs.get("session")
        session_id = session.get_session_id() if session is not None else ""
        round_state = self._round_provider(session_id)
        if round_state is None:
            return {"success": False, "status": "error", "reason": "expert_plan_unavailable"}
        query = inputs.get("query") if isinstance(inputs, dict) else None
        return await round_state.compose(query if isinstance(query, str) else "")

    async def stream(self, inputs: Any, **kwargs: Any):
        raise NotImplementedError("ExpertPlanTool uses invoke")


class DynamicExpertRail(DeepAgentRail):
    """Refresh the live task_tool menu and enforce planned expert dependencies."""

    # After ProgressiveToolRail (90) but before StreamEventRail (80): expose the
    # freshly bound task_tool and record the actual downstream task context.
    priority = 81

    def __init__(self, round_provider: Callable[[str], DynamicExpertRound | None]) -> None:
        super().__init__()
        self._round_provider = round_provider
        self._agent: Any = None

    def init(self, agent: Any) -> None:
        self._agent = agent

    def uninit(self, agent: Any) -> None:
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is not None:
            builder.remove_section(_PROMPT_SECTION)
        self._agent = None

    def _round(self, ctx: AgentCallbackContext) -> DynamicExpertRound | None:
        session = getattr(ctx, "session", None)
        if session is None:
            return None
        return self._round_provider(session.get_session_id())

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", "") or "")

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        round_state = self._round(ctx)
        builder = getattr(self._agent, "system_prompt_builder", None)
        tools = list(ctx.inputs.tools or [])
        tools = [tool for tool in tools if self._tool_name(tool) != PLAN_TOOL_NAME]
        in_plan_mode = False
        if round_state is not None and self._agent is not None:
            try:
                in_plan_mode = self._agent.load_state(ctx.session).plan_mode.mode == "plan"
            except (AttributeError, KeyError):
                pass
        if round_state is None or in_plan_mode:
            if builder is not None:
                builder.remove_section(_PROMPT_SECTION)
            ctx.inputs.tools = tools
            return

        if round_state.plan is not None:
            tools = [tool for tool in tools if self._tool_name(tool) not in _SKILL_GRAPH_TOOLS]
            if builder is not None:
                builder.remove_section("symphony_orchestration")
        if not round_state.attempted or round_state.plan is not None:
            names = ["task_tool"] if round_state.plan is not None else [PLAN_TOOL_NAME]
            refreshed = await self._agent.ability_manager.list_tool_info(names=names)
            for info in refreshed:
                tools = [tool for tool in tools if self._tool_name(tool) != info.name]
                tools.append(info)
        if builder is not None:
            builder.remove_section(_PROMPT_SECTION)
            guidance = round_state.guidance()
            builder.add_section(PromptSection(
                name=_PROMPT_SECTION,
                content={"cn": guidance, "en": guidance},
                priority=42,
            ))
        ctx.inputs.tools = tools

    @staticmethod
    def _arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @staticmethod
    def _reject(ctx: AgentCallbackContext, reason: str) -> None:
        call = ctx.inputs.tool_call
        call_id = str(getattr(call, "id", "") or "")
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = {"success": False, "error": reason}
        ctx.inputs.tool_msg = ToolMessage(content=reason, tool_call_id=call_id)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.extra.get("_skip_tool") or ctx.inputs.tool_name != "task_tool":
            return
        args = self._arguments(ctx.inputs.tool_args)
        subagent_type = str(args.get("subagent_type") or "")
        if not subagent_type.startswith(_EXPERT_TYPE_PREFIX):
            return
        round_state = self._round(ctx)
        if round_state is None or round_state.plan is None:
            self._reject(ctx, "当前没有可执行的动态专家计划")
            return
        call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
        if not call_id:
            self._reject(ctx, "动态专家调用缺少工具调用 ID")
            return
        expert_id, task = await round_state.reserve(
            subagent_type,
            str(args.get("task_description") or ""),
            call_id,
            resume=isinstance(args.get("query"), InteractiveInput),
        )
        if expert_id is None:
            self._reject(ctx, task)
            return
        if isinstance(args.get("query"), InteractiveInput):
            # Core supplies an object with the user's interrupt answers. Keep
            # it intact so TaskTool resumes the already-mounted child.
            ctx.inputs.tool_args = args
        else:
            args["task_description"] = task
            args["query"] = task
            ctx.inputs.tool_args = json.dumps(args, ensure_ascii=False)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        # Parallel tool calls share ctx.extra in Core. Use this call's own ID
        # so one expert cannot consume another expert's completion marker.
        call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
        round_state = self._round(ctx)
        if not call_id or round_state is None:
            return
        result = ctx.inputs.tool_result
        if isinstance(result, dict) and result.get("result_type") == "interrupt" and "interrupt_ids" in result:
            await round_state.suspend(call_id)
            return
        data = getattr(result, "data", None)
        output = str(data.get("output") or "") if isinstance(data, dict) else ""
        await round_state.finish(call_id, success=getattr(result, "success", False) is True, output=output)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
        round_state = self._round(ctx)
        if call_id and round_state is not None:
            await round_state.finish(call_id, success=False)


__all__ = ["DynamicExpertRail", "DynamicExpertRound", "ExpertPlanTool", "LocalExpert", "PLAN_TOOL_NAME", "scan_local_experts"]
