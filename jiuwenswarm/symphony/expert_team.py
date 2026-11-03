"""Local expert discovery and task-scoped Symphony execution binding."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
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
from openjiuwen.symphony import ExpertCandidate, plan_experts

from jiuwenswarm.common.utils import get_agent_experts_dir, get_expert_cache_dir
from jiuwenswarm.server.runtime.expert.expert_store import (
    CachedExpertPackageSource,
    LocalDirExpertPackageSource,
)
from jiuwenswarm.symphony.expert_skills import DynamicExpertSkillRail, snapshot_public_skills
from jiuwenswarm.symphony.expert_notice import ExpertNoticeState


logger = logging.getLogger(__name__)
PLAN_TOOL_NAME = "symphony_plan_experts"
CONFIRMATION_STATE_KEY = "__symphony_expert_confirmation__"
CONFIRMATION_NONCE_KEY = "__symphony_confirmation_nonce__"
EXPERT_PLAN_ACTION_KEY = "__symphony_expert_plan_action__"
_EXPERT_TYPE_PREFIX = "symphony_expert_"
_MAX_DISPLAY_SUMMARY_LENGTH = 80
_PROMPT_SECTION = "symphony_expert_turn"
_SKILL_GRAPH_TOOLS = frozenset({
    "symphony_compose_score", "symphony_refresh_score",
    "symphony_compose_graph", "symphony_refresh_graph",
})
_PARENT_SCOPED_TOOLS = frozenset({
    "task_tool", "agent", "task", "sessions_spawn", "skill_tool", "list_skill", "skill_index", "skill_index_build",
    "enter_plan_mode", "exit_plan_mode", "switch_mode",
})

_COMPETING_DELEGATION_TOOLS = frozenset({"Agent", "sessions_spawn"}) | _SKILL_GRAPH_TOOLS


def _in_plan_mode(agent: Any, session: Any) -> bool:
    try:
        return agent.load_state(session).plan_mode.mode == "plan"
    except (AttributeError, KeyError):
        return False


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

    def __init__(
        self, agent: Any, query: str, model: Any, *, session_id: str = "",
        handoff_expert: Callable[[DynamicExpertRound, LocalExpert], Awaitable[list[str]]] | None = None,
        supports_user_interaction: bool = True,
    ) -> None:
        self.agent = agent
        self.query = query
        self._base_query = ""
        self._revision_feedback: list[str] = []
        self.model = model
        self.session_id = session_id
        self._handoff_expert = handoff_expert
        self.supports_user_interaction = supports_user_interaction
        self._handoff_tool_names: list[str] = []
        self.plan: dict[str, Any] | None = None
        self.pending_plan: dict[str, Any] | None = None
        self.candidate_count = 0
        self.notice_state = ExpertNoticeState()
        self.feedback = ""
        self._selected_experts: dict[str, LocalExpert] = {}
        self._selected_configs: dict[str, SubAgentConfig] = {}
        self._display_summaries: dict[str, str] = {}
        self._planner_task: asyncio.Task[Any] | None = None
        self._confirmation: dict[str, str] = {}
        self._confirmation_session: Any = None
        self._confirmation_submitted = False
        self._confirmation_nonce = ""
        self.response: dict[str, Any] | None = None
        self._refs: list[Any] = []
        self._skill_rails: list[DynamicExpertSkillRail] = []
        self._planning_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._bindings: dict[str, str] = {}
        self._predecessors: dict[str, set[str]] = {}
        self._states: dict[str, str] = {}
        self._completed: dict[str, str] = {}
        self._dispatches: dict[str, dict[str, Any]] = {}
        self._task_ids: dict[str, str] = {}
        self._task_manager: Any = None
        self._task_scope: Any = None
        self._unsubscribe_tasks: Callable[[], None] | None = None
        self._planning_batch_delegation_ids: set[str] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def attempted(self) -> bool:
        return self.response is not None

    @property
    def closed(self) -> bool:
        return self._closed

    async def compose(self, query: str) -> dict[str, Any]:
        """Prepare one plan without binding or executing any experts."""
        async with self._planning_lock:
            if self._closed:
                return {"success": False, "status": "error", "reason": "round_closed"}
            if self.response is not None:
                return self.response
            try:
                if not self._base_query:
                    self._base_query = query.strip() or self.query
                experts = await scan_local_experts()
                configs: dict[str, SubAgentConfig] = {}
                for expert_id, expert in experts.items():
                    try:
                        configs[expert_id] = _subagent_config(self.agent, expert, self.model)
                    except Exception as exc:  # noqa: BLE001 - skip only this unusable package
                        _warn("[symphony.experts] invalid local expert %s: %s", expert_id, exc)
                available = [expert for expert_id, expert in experts.items() if expert_id in configs]
                self.candidate_count = len(available)
                self._planner_task = asyncio.create_task(plan_experts(
                    query.strip() or self.query,
                    [ExpertCandidate(e.id, e.name, e.description) for e in available],
                    self.model,
                ))
                try:
                    plan = await self._planner_task
                finally:
                    self._planner_task = None
                if plan["status"] == "no_plan":
                    self.response = {
                        "success": True, **plan,
                        "reason": "no_available_experts" if not available else "no_suitable_plan",
                    }
                    return self.response
                if plan["status"] != "ready":
                    raise ValueError(f"invalid expert plan status: {plan['status']}")

                self.query = query.strip() or self.query
                self.pending_plan = plan["graph"]
                summaries = plan.get("display_summaries")
                self._display_summaries = dict(summaries) if isinstance(summaries, dict) else {}
                self._selected_experts = {key: experts[key] for key in self.pending_plan["nodes"]}
                self._selected_configs = {key: configs[key] for key in self.pending_plan["nodes"]}
                self.response = {"success": True, "status": "awaiting_confirmation"}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep ordinary chat available
                _warn("[symphony.experts] planning failed: %s", exc)
                self.pending_plan = None
                self.response = {
                    "success": False, "status": "error",
                    "reason": "expert_planning_failed", "detail": str(exc),
                }
            return self.response

    async def revise_plan(
        self, session_id: str, call_id: str, metadata: dict[str, Any],
        confirmation_nonce: str, feedback: str,
    ) -> dict[str, Any]:
        """Discard the displayed plan and prepare a new one from user feedback."""
        async with self._planning_lock:
            if (not feedback.strip() or not self.pending_plan
                    or not self.matches_confirmation_resume(
                        session_id, call_id, metadata, confirmation_nonce,
                    )):
                return {"success": False, "status": "error", "reason": "expert_confirmation_expired"}
            self.clear_confirmation_state()
            self._confirmation = {}
            self._confirmation_submitted = False
            self.pending_plan = None
            self.response = None
            self._selected_experts = {}
            self._selected_configs = {}
            self._display_summaries = {}
            self.feedback = feedback.strip()
            self._revision_feedback.append(self.feedback)
            query = self._base_query + "\n\n用户对专家协作方案的补充要求：\n" + "\n".join(
                f"{index}. {item}" for index, item in enumerate(self._revision_feedback, 1)
            )
        return await self.compose(query)

    def confirmation_display(self, plan_id: str) -> dict[str, Any]:
        """Describe only the frozen selected packages for the pending confirmation."""
        assert self.pending_plan is not None
        single_expert = len(self.pending_plan["nodes"]) == 1
        members = []
        for expert_id, node in self.pending_plan["nodes"].items():
            expert = self._selected_experts[expert_id]
            member = {
                "id": expert_id, "name": node["label"], "source": expert.source,
            }
            if single_expert:
                # Reuse the card's summary field for the expert introduction.
                # The desktop accepts at most 80 Unicode code points.
                summary = expert.description.strip()
                if len(summary) > _MAX_DISPLAY_SUMMARY_LENGTH:
                    summary = summary[:_MAX_DISPLAY_SUMMARY_LENGTH - 1].rstrip() + "…"
            else:
                summary = self._display_summaries.get(expert_id, "")
            if summary:
                member["summary"] = summary
            if expert.avatar:
                member["avatar"] = expert.avatar
            members.append(member)
        if single_expert:
            display = {
                "title": f"让「{members[0]['name']}」接着帮你？",
                "description": (
                    "小艺Work为你找到了这位专家。你同意后，专家会接着这段对话把任务做下去，"
                    "之后也会继续回复你。想换回小艺Work，随时可以在输入框选择。"
                ),
                "feedback_placeholder": "想换位专家或补充要求？告诉小艺Work…",
            }
        else:
            display = {
                "title": "请这些专家一起帮你？",
                "description": (
                    "小艺Work为你找到了几位专家。你同意后，小艺Work作为团队主理人，"
                    "会协调他们按下面的分工一起完成这件事。"
                ),
                "feedback_placeholder": "想调整人选或分工？告诉小艺Work…",
            }
        if 1 <= self.candidate_count <= 5:
            display["candidate_hint"] = "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。"
        return {
            "plan_id": plan_id, "candidate_count": self.candidate_count,
            "members": members, "display": display,
        }

    def request_confirmation(
        self, session_id: str, call_id: str, *, session: Any = None,
    ) -> dict[str, str] | None:
        """Tie the saved plan to one live session and the original tool call."""
        if self.pending_plan is None:
            return None
        return self._register_confirmation("dynamic_expert_plan_id", session_id, call_id, session)

    def request_no_plan_confirmation(
        self, session_id: str, call_id: str, *, session: Any = None,
    ) -> dict[str, str] | None:
        """Ask once for an eligible no-plan result without changing other flows."""
        if (self.pending_plan is not None
                or not self.response or self.response.get("status") != "no_plan"
                or self.response.get("decision") is not None
                or not self.supports_user_interaction):
            return None
        return self._register_confirmation("dynamic_expert_no_plan_id", session_id, call_id, session)

    def _register_confirmation(
        self, identity_key: str, session_id: str, call_id: str, session: Any,
    ) -> dict[str, str] | None:
        """Register one server-owned identity without resetting its admission nonce."""
        if self._closed or not session_id or not call_id:
            return None
        if self.session_id and self.session_id != session_id:
            return None
        if self._confirmation and (
            self._confirmation["tool_call_id"] != call_id or identity_key not in self._confirmation
        ):
            return None
        if not self._confirmation:
            self.session_id = session_id
            self._confirmation = {
                "session_id": session_id, "tool_call_id": call_id, identity_key: uuid4().hex,
            }
            if session is not None:
                self._confirmation_session = session
                session.update_state({CONFIRMATION_STATE_KEY: dict(self._confirmation)})
        return dict(self._confirmation)

    def clear_confirmation_state(self) -> None:
        self._confirmation_nonce = ""
        session = self._confirmation_session
        if session is not None and session.get_state(CONFIRMATION_STATE_KEY) == self._confirmation:
            session.update_state({CONFIRMATION_STATE_KEY: None})
        self._confirmation_session = None

    def matches_confirmation(self, session_id: str, call_id: str, metadata: dict[str, Any]) -> bool:
        return bool(
            not self._closed and self._confirmation
            and self.session_id == session_id
            and self._confirmation["tool_call_id"] == call_id
            and all(metadata.get(key) == value for key, value in self._confirmation.items())
        )

    def claim_confirmation_resume(self, session_id: str, call_id: str, metadata: dict[str, Any]) -> str | None:
        """Issue a one-time credential for the validated input, before Core consumes it."""
        if self._confirmation_submitted or not self.matches_confirmation(session_id, call_id, metadata):
            return None
        self._confirmation_submitted = True
        self._confirmation_nonce = uuid4().hex
        return self._confirmation_nonce

    def matches_confirmation_resume(
        self, session_id: str, call_id: str, metadata: dict[str, Any], nonce: Any,
    ) -> bool:
        return bool(
            self._confirmation_nonce and nonce == self._confirmation_nonce
            and self.matches_confirmation(session_id, call_id, metadata)
        )

    async def resolve_no_plan_choice(
        self, session_id: str, call_id: str, metadata: dict[str, Any],
        confirmation_nonce: str, decision: str,
    ) -> dict[str, Any]:
        """Consume the saved choice once, leaving a durable tool result."""
        async with self._planning_lock:
            if (decision not in {"continue", "summon"}
                    or not self.matches_confirmation_resume(
                        session_id, call_id, metadata, confirmation_nonce,
                    )
                    or self.pending_plan is not None
                    or not self.response or self.response.get("status") != "no_plan"
                    or not metadata.get("dynamic_expert_no_plan_id")):
                return {"success": False, "status": "error", "reason": "expert_confirmation_expired"}
            self.response = {
                **self.response,
                "decision": decision,
                "confirmation_id": metadata["dynamic_expert_no_plan_id"],
                "candidate_count": self.candidate_count,
            }
            self.clear_confirmation_state()
            self._confirmation = {}
            return self.response

    async def confirm_and_bind(
        self, session_id: str, call_id: str, metadata: dict[str, Any], *, confirmation_nonce: str,
    ) -> dict[str, Any]:
        """Bind exactly the saved templates after the confirmation rail approves."""
        async with self._planning_lock:
            if not self.matches_confirmation_resume(session_id, call_id, metadata, confirmation_nonce):
                return {"success": False, "status": "error", "reason": "expert_confirmation_expired"}
            self._confirmation_nonce = ""
            if self.pending_plan is None:
                return self.response or {"success": False, "status": "error", "reason": "round_closed"}
            graph = self.pending_plan
            nodes = graph["nodes"]
            if len(nodes) == 1:
                return await self._confirm_single_expert(metadata["dynamic_expert_plan_id"])
            refs = []
            try:
                # The pinned Core rolls back a partially applied batch itself.
                bind_task = asyncio.create_task(apply_extension_hot(
                    self.agent, ExtensionParts(subagents=list(self._selected_configs.values())),
                ))
                try:
                    refs = await asyncio.shield(bind_task)
                except asyncio.CancelledError:
                    # Do not abandon resources if cancellation arrives during bind.
                    try:
                        refs = await bind_task
                        await unapply_extension_hot(self.agent, refs)
                    except Exception as exc:  # noqa: BLE001 - retain cancellation
                        _warn("[symphony.experts] cancelled bind cleanup failed: %s", exc)
                    raise
                if self.agent.ability_manager.get("task_tool") is None:
                    raise RuntimeError("当前代理无法使用 task_tool")
                skill_rails = [
                    rail for config in self._selected_configs.values()
                    for rail in getattr(config, "rails", []) or []
                    if isinstance(rail, DynamicExpertSkillRail)
                ]
                predecessors = {expert_id: set() for expert_id in nodes}
                for edge in graph["edges"]:
                    predecessors[edge["target"]].add(edge["source"])
                bindings = {expert_id: _subagent_type(expert_id) for expert_id in nodes}
                response = {
                    "success": True,
                    "status": "ready",
                    "planned_graph": {"graph": graph},
                    "bindings": dict(bindings),
                    "expert_assets": {
                        expert_id: {
                            "source": expert.source,
                            **({"avatar": expert.avatar} if expert.avatar else {}),
                        }
                        for expert_id, expert in self._selected_experts.items()
                    },
                }
                self._refs = refs
                self._skill_rails = skill_rails
                self.plan = graph
                self._bindings = bindings
                self._states = {expert_id: "pending" for expert_id in nodes}
                self._predecessors = predecessors
                self.response = response
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep ordinary chat available
                if refs:
                    await unapply_extension_hot(self.agent, refs)
                _warn("[symphony.experts] binding failed: %s", exc)
                self.response = {
                    "success": False, "status": "error",
                    "reason": "expert_binding_failed", "detail": str(exc),
                }
            self.pending_plan = None
            self._selected_configs = {}
            self._selected_experts = {}
            self.clear_confirmation_state()
            return self.response

    async def _confirm_single_expert(self, plan_id: str) -> dict[str, Any]:
        """Transfer the confirmed package to the current session, never a child agent."""
        expert = next(iter(self._selected_experts.values()))
        try:
            if self._handoff_expert is None:
                raise RuntimeError("expert_handoff_unavailable")
            self._handoff_tool_names = await self._handoff_expert(self, expert)
            self.response = {
                "success": True, "status": "handed_off", "plan_id": plan_id,
                "expert": {
                    "id": expert.id, "name": expert.name, "source": expert.source,
                    **({"avatar": expert.avatar} if expert.avatar else {}),
                },
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the adapter rolls back before returning
            _warn("[symphony.experts] session handoff failed: %s", exc)
            self.response = {
                "success": False, "status": "handoff_failed", "plan_id": plan_id, "reason": str(exc),
            }
        finally:
            self.pending_plan = None
            self._selected_configs = {}
            self._selected_experts = {}
            self.clear_confirmation_state()
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

    def bind_task_manager(self, manager: Any) -> None:
        if self._task_manager is manager:
            return
        if self._task_manager is not None:
            raise ValueError("本轮专家的任务工具已更换，请重新发起任务")
        self._task_scope = manager.scope_for_session(self.session_id)
        self._task_manager = manager
        self._unsubscribe_tasks = manager.add_task_listener(self._on_task_state)

    def _on_task_state(self, scope: Any, record: dict[str, Any]) -> None:
        if scope != self._task_scope:
            return
        batch = self._dispatches.get(record.get("dispatch_tool_call_id"))
        index = record.get("dispatch_index")
        if batch is None or not isinstance(index, int) or not 0 <= index < len(batch["experts"]):
            return
        batch["started"] = True
        expert_id = batch["experts"][index]
        self._task_ids[record["task_id"]] = expert_id
        status = record.get("status")
        self._states[expert_id] = "interrupted" if status == "waiting_approval" else status
        if status == "completed":
            self._completed[expert_id] = str(record.get("output") or "")

    async def prepare_dispatch(self, call_id: str, requests: list[Any], task_ids: list[str]) -> list[dict[str, Any]]:
        """Reserve the whole validated batch atomically, before Core starts workers."""
        async with self._state_lock:
            if self._closed or self.plan is None:
                raise ValueError("本轮专家计划已结束")
            if any(key not in self._task_ids for key in task_ids):
                raise ValueError("只能等待或取消本轮专家任务")
            signature = (tuple(requests), tuple(sorted(task_ids)))
            previous = self._dispatches.get(call_id)
            if previous is not None:
                if signature not in (previous["original"], previous["rewritten"]):
                    raise ValueError("同一工具调用不能改派其他专家任务")
                return previous["tasks"]
            experts, tasks = [], []
            for request in requests:
                expert_id = next((key for key, value in self._bindings.items()
                                  if value == request.subagent_type), None)
                if expert_id is None:
                    raise ValueError("动态专家协作只能执行已确认方案中的专家节点")
                if expert_id in experts or self._states[expert_id] != "pending":
                    raise ValueError("该专家节点已调用，不可重复执行")
                if any(self._states[key] in {"failed", "timed_out", "cancelled"}
                       for key in self._predecessors[expert_id]):
                    raise ValueError("前置专家执行失败，不能执行该节点")
                if not self._predecessors[expert_id] <= self._completed.keys():
                    raise ValueError("前置专家尚未完成，请按计划依赖顺序调用")
                experts.append(expert_id)
                tasks.append({"subagent_type": request.subagent_type,
                              "task_description": self._task_context(expert_id, request.task_description)})
            # No await or state mutation until every task is accepted.
            rewritten = tuple(replace(request, task_description=task["task_description"])
                              for request, task in zip(requests, tasks))
            self._dispatches[call_id] = {"original": signature, "rewritten": (rewritten, signature[1]),
                                         "experts": experts, "tasks": tasks, "started": False}
            for expert_id in experts:
                self._states[expert_id] = "running"
            return tasks

    def rollback_dispatch(self, call_id: str) -> None:
        batch = self._dispatches.get(call_id)
        if batch is not None and not batch["started"]:
            for expert_id in batch["experts"]:
                self._states[expert_id] = "pending"
            self._dispatches.pop(call_id)

    def guidance(self) -> str:
        if self.pending_plan is not None:
            return "专家协作方案正在等待用户确认。确认前不得调用 task_tool 或声称专家已执行。"
        if self.response is not None and self.response.get("status") == "handed_off":
            return "已切换到用户确认的专家。请以当前专家身份和已加载的工具、技能继续完成原任务。"
        if self.response is not None and self.response.get("status") == "handoff_failed":
            return "专家切换失败，本轮已停止。请提示用户重试，等待新的用户请求。"
        if self.response is not None and self.response.get("decision") == "summon":
            return "用户选择先去召唤专家，本轮已结束。等待新的用户请求。"
        if self.plan is None:
            if self.response is None:
                return (
                    "仅当用户当前请求使用产品内的 AI 专家处理当前任务或临时组织专家协作时，"
                    "调用 symphony_plan_experts；结合完整上下文判断，“这些专家”等指代可由前文明确。"
                    "“使用动态专家团”“用专家团帮我完成这件事”“让几位专家一起分析”等均是调用意图，"
                    "不要求出现“调用”二字。"
                    "区分当前任务协作与专家包创建：要求创建、修改、保存或打包可复用的专家/专家团时，"
                    "应使用当前可用的专家创建/管理 skill（如 expert-manager、expert-manager-win），"
                    "不要为此调用本工具；临时协作不需要创建专家包。"
                    "浏览、查找或安装专家不代表执行当前任务。不要只按“专家”“组建”“创建”等词触发。"
                    "现实中的医生、律师、评审人等，不等于产品 AI 专家；角色扮演、功能咨询、"
                    "引用或翻译文本、否定和假设仅被提及时，不构成调用意图。"
                    "仅在完整上下文仍无法区分当前任务协作与其他意图时才澄清。"
                    "未明确要求时，沿用常规处理流程。已召唤固定专家团时沿用当前团队流程。"
                )
            if self.response.get("status") == "no_plan":
                return (
                    "服务已向用户显示本次未生成专家方案的提示，不要重复提示或等待用户选择。"
                    "请沿用常规处理流程继续完成用户任务，不要声称专家已执行。"
                )
            if self.response.get("status") == "declined":
                return "用户选择不使用专家。请由小艺Work独立完成原任务，本轮不要再次规划专家。"
            return "本轮没有可执行的专家计划。请沿用常规处理流程继续完成用户任务，不要声称专家已执行。"

        lines = [
            "按 planned_graph 依赖顺序调用真实 task_tool，完成本轮专家委派；"
            "将当前依赖已就绪的独立节点放入一次 dispatch 的 tasks；后续用 task_ids 等待或取消本轮任务。工具会阻止越序和重复调用。"
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
        elif any(state in {"failed", "timed_out", "cancelled"} for state in self._states.values()):
            lines.append("已有专家失败。仅继续执行不依赖失败节点的任务，并在最终答复中说明未执行部分。")
        return "\n".join(lines)

    async def close(self) -> None:
        # Own the whole close operation, not only each skill unload. A second
        # cancellation must not abandon the remaining children or parent refs.
        if self._planner_task is not None:
            self._planner_task.cancel()
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
                self.pending_plan = None
                self._selected_configs = {}
                self._selected_experts = {}
                self._display_summaries = {}
                self.clear_confirmation_state()
                self._confirmation = {}
                refs, self._refs = self._refs, []
        # Core owns child lifetime; cancel only tasks bound to this expert round.
        try:
            if (self._task_manager is not None and self._task_scope is not None and self._task_ids
                    and self._task_manager.scope_for_run(self._task_scope.run_id) is not None):
                await self._task_manager.cancel(self._task_scope, list(self._task_ids))
        except Exception as exc:  # noqa: BLE001 - still unload rails and bindings
            _warn("[symphony.experts] failed to drain expert tasks: %s", exc)
        finally:
            if self._unsubscribe_tasks is not None:
                self._unsubscribe_tasks()
                self._unsubscribe_tasks = None
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
    """Real model-callable planning tool, scoped to an active single-agent request."""

    def __init__(self, round_provider: Callable[[str], DynamicExpertRound | None]) -> None:
        super().__init__(ToolCard(
            id=PLAN_TOOL_NAME,
            name=PLAN_TOOL_NAME,
            description=(
                "Plan delegation only when the user's current request asks to use this product's AI "
                "experts or organize temporary AI expert collaboration for the current task. Full context "
                "may establish which product experts a reference such as 'those experts' means. Requests such as "
                "'使用动态专家团', '用专家团帮我完成这件事', or '让几位专家一起分析' express this intent; "
                "the verb 'invoke' is not required. Creating, editing, saving, or packaging reusable expert "
                "agents or teams belongs to the available expert creation/management skill "
                "(e.g. expert-manager or expert-manager-win), not this tool. Temporary collaboration "
                "does not require creating expert packages. Browsing, finding, or installing experts is "
                "not task execution. Real-world doctors, lawyers, or reviewers are not product AI experts. "
                "Roleplay, functional questions, quoted or translated text, negation, and hypotheticals "
                "do not by themselves request invocation. Judge the full intent, not individual keywords; "
                "clarify only if the full context still leaves the intent ambiguous. "
                "Prepares a plan and waits for explicit user confirmation before binding experts. "
                "If the user declines, continue the original task without experts. "
                "If the user provides feedback, revise the plan and request confirmation again. "
                "After confirmation, a single expert returns handed_off and becomes this session's identity; "
                "continue the original task directly with that expert's loaded tools and skills. "
                "Multiple experts return a planned_graph and task_tool subagent_type names. "
                "handoff_failed stops the turn and requires the user to retry. Returns no_plan if no usable "
                "expert plan is available. On no_plan, follow the user's returned choice: "
                "continue the normal task or end this turn to summon more experts. "
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
        if round_state is None or round_state.closed or _in_plan_mode(round_state.agent, session):
            return {"success": False, "status": "error", "reason": "expert_plan_unavailable"}
        if (round_state.session_id != session_id or round_state.pending_plan is not None
                or "dynamic_expert_no_plan_id" in round_state._confirmation):
            return {"success": False, "status": "error", "reason": "expert_confirmation_required"}
        return round_state.response or {
            "success": False, "status": "error", "reason": "expert_confirmation_required",
        }

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
        in_plan_mode = _in_plan_mode(self._agent, ctx.session)
        if round_state is None or in_plan_mode:
            if builder is not None:
                builder.remove_section(_PROMPT_SECTION)
            ctx.inputs.tools = tools
            return

        if round_state.pending_plan is not None or round_state.plan is not None:
            tools = [tool for tool in tools if self._tool_name(tool) not in _COMPETING_DELEGATION_TOOLS]
            if builder is not None:
                builder.remove_section("symphony_orchestration")
        names = []
        if not round_state.attempted or round_state.plan is not None:
            names = ["task_tool"] if round_state.plan is not None else [PLAN_TOOL_NAME]
        elif round_state.response is not None and round_state.response.get("status") == "handed_off":
            # ReAct captured this invoke's tool menu before resuming the paused
            # confirmation. Refresh only tools added by the selected package.
            names = round_state._handoff_tool_names
        if names:
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

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        round_state = self._round(ctx)
        if round_state is None:
            return
        # Core starts all tool calls in this response concurrently. Capture the
        # whole batch before any planner/discovery await, regardless of order.
        # Keep IDs on the round across HITL retries; the next model response
        # replaces them so ordinary delegation is available after decline/no_plan.
        calls = getattr(ctx.inputs.response, "tool_calls", None) or []
        round_state._planning_batch_delegation_ids = {
            str(call.id or "") for call in calls
            if call.name in _COMPETING_DELEGATION_TOOLS or call.name == "task_tool"
        } if any(call.name == PLAN_TOOL_NAME for call in calls) else set()

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
        if ctx.extra.get("_skip_tool"):
            return
        round_state = self._round(ctx)
        tool_name = ctx.inputs.tool_name
        call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
        if (round_state is not None and call_id in round_state._planning_batch_delegation_ids
                and (tool_name in _COMPETING_DELEGATION_TOOLS or tool_name == "task_tool")):
            self._reject(ctx, "请先完成本批动态专家规划与确认，再按确认后的方案执行")
            return
        if round_state is not None and (round_state.pending_plan is not None or round_state.plan is not None):
            if tool_name in _COMPETING_DELEGATION_TOOLS:
                self._reject(ctx, "动态专家协作须按已确认方案通过 task_tool 执行")
                return
        if tool_name != "task_tool":
            return
        args = self._arguments(ctx.inputs.tool_args)
        tasks = args.get("tasks")
        requests_expert = (isinstance(tasks, list) and any(
            isinstance(task, dict) and str(task.get("subagent_type", "")).startswith(_EXPERT_TYPE_PREFIX)
            for task in tasks
        )) or str(args.get("subagent_type", "")).startswith(_EXPERT_TYPE_PREFIX)
        active_plan = round_state is not None and (round_state.pending_plan is not None or round_state.plan is not None)
        if not active_plan and not requests_expert:
            return
        if _in_plan_mode(self._agent, ctx.session):
            self._reject(ctx, "请先通过计划审批退出只读规划模式，再执行动态专家任务")
            return
        if round_state is None or round_state.plan is None:
            self._reject(ctx, "当前没有可执行的动态专家计划")
            return
        if not call_id:
            self._reject(ctx, "动态专家调用缺少工具调用 ID")
            return
        try:
            card = self._agent.ability_manager.get("task_tool")
            tool = Runner.resource_mgr.get_tool(card.id) if isinstance(card, ToolCard) else None
            if tool is None:
                raise ValueError("当前代理无法使用 task_tool")
            action, requests, task_ids, _timeout = tool.parse_group_inputs(args)
            round_state.bind_task_manager(tool.task_manager)
            if action == "dispatch":
                prepared = await round_state.prepare_dispatch(call_id, requests, task_ids)
                args["tasks"] = [{**task, **context} for task, context in zip(args["tasks"], prepared)]
                # Both execution and subsequent rails must retain Core's resume input.
                ctx.inputs.tool_args = args
                ctx.inputs.tool_call.arguments = args
            elif any(key not in round_state._task_ids for key in task_ids):
                raise ValueError("只能等待或取消本轮专家任务")
        except (ValueError, TypeError) as exc:
            self._reject(ctx, str(exc))

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        round_state = self._round(ctx)
        if round_state is None or ctx.inputs.tool_name != "task_tool" or ctx.extra.get("_skip_tool"):
            return
        # State/output come from task notifications, including completions that
        # are never collected by wait. A failed dispatch must release reservations.
        if getattr(ctx.inputs.tool_result, "success", None) is False:
            round_state.rollback_dispatch(str(getattr(ctx.inputs.tool_call, "id", "") or ""))

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        round_state = self._round(ctx)
        if round_state is not None and ctx.inputs.tool_name == "task_tool":
            round_state.rollback_dispatch(str(getattr(ctx.inputs.tool_call, "id", "") or ""))


__all__ = ["DynamicExpertRail", "DynamicExpertRound", "ExpertPlanTool", "LocalExpert", "PLAN_TOOL_NAME", "scan_local_experts"]
