# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Fork agent executor for DeepAgent architecture.

Core executor for fork_agent and spawn_subagent execution.
"""

from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.rails.filesystem_rail import FileSystemRail
from openjiuwen.harness.rails.skill_use_rail import SkillUseRail
from openjiuwen.harness.workspace.workspace import Workspace
from openjiuwen.core.foundation.llm import Model

from jiuwenclaw.agentserver.tools.subagent_models import (
    ForkAgentResult,
    ForkAgentTaskSpec,
    SubagentResult,
    SubagentTaskSpec,
)
from jiuwenclaw.agentserver.deep_agent.prompt_builder import build_subagent_base_prompt
from jiuwenclaw.agentserver.deep_agent.rails import JiuClawContextEngineeringRail
from jiuwenclaw.utils import (
    get_agent_registered_skill_dirs,
    get_agent_root_dir,
    logger,
)
from jiuwenclaw.config import get_config

from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    get_effective_request_workspace_dir,
    get_subagent_parent_session,
    _get_llm_trace_session_id_var,
)
from jiuwenclaw.agentserver.tools.subagent_executor.session_proxy import SubagentSessionProxy
from jiuwenclaw.agentserver.tools.subagent_executor.rails import (
    ForkMessageInjectionRail,
    SubagentContextRail,
)
from jiuwenclaw.agentserver.tools.subagent_executor.skill_use_rail_subagent import (
    SubagentSkillUseRail,
)

# Default timeout for subagent execution
_DEFAULT_TIMEOUT_SECONDS = 600.0

# Default excluded tools for spawn/fork agents
EXCLUDED_TOOLS_SPAWN = {
    "spawn_subagent",
    "send_file_to_user",
    # 主 Agent 级调度与消息（子 Agent 不应触发）
    "office_claw_dispatch_agent_task",
    "office_claw_post_message",
    "office_claw_get_pending_mentions",
    "office_claw_ack_mentions",
    "office_claw_get_thread_context",
    "office_claw_list_threads",
    "office_claw_cross_post_message",
    "office_claw_register_pr_tracking",
    "office_claw_multi_mention",
    # 主 Agent 级计划任务
    "office_claw_list_scheduled_tasks",
    "office_claw_list_schedule_templates",
    "office_claw_preview_scheduled_task",
    "office_claw_register_scheduled_task",
    "office_claw_set_scheduled_task_enabled",
    "office_claw_remove_scheduled_task",
    "office_claw_update_scheduled_task",
    # 主 Agent 级记忆与反思
    "office_claw_retain_memory_callback",
    "office_claw_search_evidence",
    "office_claw_reflect",
    # 主 Agent 级会话链追踪
    "office_claw_list_session_chain",
    "office_claw_read_session_events",
    "office_claw_read_session_digest",
    "office_claw_read_invocation_detail",
    # 主 Agent 级技能管理
    "office_claw_list_skills",
}

EXCLUDED_TOOLS_FORK = EXCLUDED_TOOLS_SPAWN | {"fork_agent"}

# Subagent ReAct cap when parent has no usable max_iterations (mirrors interface_deep fallback).
_DEFAULT_SUBAGENT_MAX_ITERATIONS = 15


class ForkAgentExecutor:
    """Fork agent executor for DeepAgent architecture.

    Uses Runner.run_agent(context=...) to pass fork_messages instead of
    modifying invoke method (which is inside DeepAgent SDK).
    """

    def __init__(
        self,
        parent_agent: DeepAgent,
        model: Model,
        default_role_prompts: dict[str, str] | None = None,
    ) -> None:
        """Initialize the subagent executor.

        Args:
            parent_agent: Parent DeepAgent instance (for inheriting tools)
            model: Model instance for creating subagents
            default_role_prompts: Default role prompts (used when role_id not found)
        """
        self._parent_agent = parent_agent
        self._model = model
        self._default_role_prompts = default_role_prompts or {}
        self._active_fork_agents: dict[str, Any] = {}  # task_id -> subagent instance

    def _resolve_subagent_workspace_dir(self) -> tuple[str, str]:
        """Resolve workspace for fork/spawn to match the main agent for the current request.

        Order: per-request (same as RuntimePromptRail) > parent DeepAgent workspace > agent root.
        """
        req_ws = get_effective_request_workspace_dir()
        if isinstance(req_ws, str) and req_ws.strip():
            return (req_ws.strip(), "effective_request_workspace_dir")

        parent_config = getattr(self._parent_agent, "deep_config", None)
        if parent_config and hasattr(parent_config, "workspace"):
            parent_ws = getattr(parent_config.workspace, "root_path", None)
            if parent_ws:
                root = str(parent_ws).strip()
                if root:
                    return (root, "parent_config.workspace.root_path")

        return (get_agent_root_dir(), "get_agent_root_dir()")

    def _resolve_subagent_max_iterations(self) -> int:
        """Use parent DeepAgent's max_iterations; then react.max_iterations; then default.

        Keeps spawn/fork subagents aligned with the main agent cap (see interface_deep react).
        """
        parent = self._parent_agent
        dc = getattr(parent, "deep_config", None)
        if dc is not None:
            raw = getattr(dc, "max_iterations", None)
            if raw is not None:
                try:
                    n = int(raw)
                    if n > 0:
                        return n
                except (TypeError, ValueError):
                    pass

        cfg = get_config()
        react = cfg.get("react") if isinstance(cfg.get("react"), dict) else {}
        raw = react.get("max_iterations")
        if raw is not None and raw != "":
            try:
                n = int(raw)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass

        return _DEFAULT_SUBAGENT_MAX_ITERATIONS

    def _parent_has_filesystem_rail(self) -> bool:
        """Return whether the parent agent exposes local file tools through FileSystemRail."""
        parent_config = getattr(self._parent_agent, "deep_config", None)
        candidate_rails = []
        for source in (
            getattr(parent_config, "rails", None),
            getattr(self._parent_agent, "rails", None),
            getattr(self._parent_agent, "_rails", None),
        ):
            if source:
                candidate_rails.extend(source)

        return any(type(rail).__name__ == "FileSystemRail" for rail in candidate_rails)

    def _build_inherited_filesystem_rail(self) -> FileSystemRail | None:
        """Create a child FileSystemRail when the parent had one.

        FileSystemRail tools are not necessarily present in ability_manager, so spawn/fork must
        mount their own rail instead of relying only on ToolCard inheritance.
        """
        if not self._parent_has_filesystem_rail():
            return None

        try:
            return FileSystemRail()
        except Exception as exc:
            logger.warning("[Subagent] FileSystemRail inheritance failed: %s", exc)
            return None

    def resolve_permission_approval(self, request_id: str, answers: list) -> bool:
        """Resolve permission approval across all active fork agents.

        Called by parent agent when it cannot find the request_id in its own pending approvals.

        Args:
            request_id: Permission approval request ID
            answers: User's answers from the approval UI

        Returns:
            True if resolved successfully, False otherwise
        """
        for task_id, fork_agent in self._active_fork_agents.items():
            resolve_method = getattr(fork_agent, "_resolve_permission_approval", None)
            if resolve_method is not None:
                try:
                    resolved = resolve_method(request_id, answers)
                    if resolved:
                        logger.info(
                            f"[ForkAgent] Resolved permission approval in "
                            f"task_id={task_id}, request_id={request_id}"
                        )
                        return True
                except Exception as e:
                    logger.warning(
                        f"[ForkAgent] Failed to resolve permission approval "
                        f"in task_id={task_id}: {e}"
                    )
        return False

    def _get_role_definition(self, role_id: str) -> Any | None:
        """Get role definition from default_role_prompts.

        Args:
            role_id: Role ID to lookup

        Returns:
            SubagentRoleDefinition or None (triggers dynamic generation)
        """
        from jiuwenclaw.agentserver.tools.subagent_models import SubagentRoleDefinition

        if role_id in self._default_role_prompts:
            return SubagentRoleDefinition(
                name=role_id,
                system_prompt=self._default_role_prompts[role_id],
            )
        return None

    @staticmethod
    def _generate_dynamic_role_prompt(role_id: str) -> str:
        """Generate dynamic role prompt based on role name.

        Triggered when user specifies a role that's not predefined.
        Examples: "Java架构师", "数据分析师", etc.

        Args:
            role_id: User-specified role name

        Returns:
            Role-specific prompt (will be appended to build_subagent_base_prompt)
        """
        config_base = get_config()
        language = config_base.get("preferred_language", "zh")

        if language == "zh":
            return f"""你是一个 {role_id}。

以该领域的专业知识和最佳实践执行任务。你的职责包括：
- 运用领域特定的知识和最佳实践
- 提供结构化、有理据的分析和建议
- 以该领域专家应有的精确度执行任务

系统化地处理每个任务，交付高质量的结果。
"""
        else:
            return f"""You are a {role_id}.

Act with expertise and professionalism in this domain. Your responsibilities include:
- Applying domain-specific knowledge and best practices
- Providing structured, well-reasoned analysis and recommendations
- Executing tasks with the precision expected of an expert in this field

Approach each task methodically and deliver high-quality results.
"""

    async def execute_fork(
        self,
        task: ForkAgentTaskSpec,
        fork_messages: list[Any],
        parent_session: Session | None = None,
    ) -> ForkAgentResult:
        """Execute a fork agent task with inherited messages.

        Key mechanism:
        - ForkMessageInjectionRail injects fork_messages at before_model_call hook
        - This works around Runner.run_agent ignoring the context parameter

        Args:
            task: Fork agent task specification
            fork_messages: Messages from parent Agent context to inherit
            parent_session: Optional parent session for event forwarding
        """
        if parent_session is None:
            parent_session = get_subagent_parent_session()

        try:
            # 1. Create session proxy FIRST (needed for SubagentContextRail to emit events)
            session_proxy: SubagentSessionProxy | None = None
            if parent_session is not None:
                session_proxy = SubagentSessionProxy(
                    parent_session=parent_session,
                    subagent_id=task.task_id,
                    role_id=task.role_id,
                )

            # 2. Create fork agent with fork_messages injection rail
            fork_agent = await self._create_fork_agent(task, fork_messages, parent_session=session_proxy)

            # 3. Build full prompt
            full_prompt = task.objective
            if task.prompt:
                full_prompt = f"{task.objective}\n\n{task.prompt}"

            logger.info(
                f"[ForkAgent] Starting execution, task_id={task.task_id}, role_id={task.role_id}, "
                f"inherited_messages={len(fork_messages)}"
            )

            # 4. Register active agent for permission approval resolution
            self._active_fork_agents[task.task_id] = fork_agent

            # 5. Set session_id for LLM IO trace logging
            if session_proxy:
                trace_session_id = session_proxy.get_session_id()
            else:
                trace_session_id = task.task_id
            llm_trace_var = _get_llm_trace_session_id_var()
            token_trace_sid = llm_trace_var.set(trace_session_id)

            # 6. Execute fork agent
            session_id = task.task_id
            invoke_inputs = {"query": full_prompt, "conversation_id": session_id}

            try:
                response = await Runner.run_agent(
                    agent=fork_agent,
                    inputs=invoke_inputs,
                    session=session_proxy,
                )
            finally:
                self._active_fork_agents.pop(task.task_id, None)
                llm_trace_var.reset(token_trace_sid)

            logger.info(f"[ForkAgent] Execution completed, task_id={task.task_id}")

            # 7. Extract result and usage
            result_text = ""
            fork_usage = None
            if isinstance(response, dict):
                result_text = response.get("output", "")
                if isinstance(result_text, dict):
                    result_text = result_text.get("output", str(result_text))
                fork_usage = response.get("usage")
            elif hasattr(response, "content"):
                result_text = response.content
            elif hasattr(response, "text"):
                result_text = response.text
            else:
                result_text = str(response)

            if fork_usage:
                logger.info(f"[ForkAgent] task_id={task.task_id} usage: {fork_usage}")

            return ForkAgentResult(
                success=True,
                task_id=task.task_id,
                role_id=task.role_id,
                result=result_text,
                usage=fork_usage,
            )

        except asyncio.TimeoutError:
            logger.warning(
                f"[ForkAgent] Timeout after {_DEFAULT_TIMEOUT_SECONDS} seconds, task_id={task.task_id}"
            )
            return ForkAgentResult(
                success=False,
                task_id=task.task_id,
                role_id=task.role_id,
                error=f"Timeout after {_DEFAULT_TIMEOUT_SECONDS} seconds",
            )
        except Exception as e:
            logger.exception(f"[ForkAgent] Execution failed: {e}")
            return ForkAgentResult(
                success=False,
                task_id=task.task_id,
                role_id=task.role_id,
                error=str(e),
            )

    async def execute_spawn(
        self,
        task: SubagentTaskSpec,
        parent_session: Session | None = None,
    ) -> SubagentResult:
        """Execute a spawn subagent task with isolated context.

        Key differences from execute_fork:
        - Uses Runner.run_agent(session=None) for isolated context
        - No fork_messages passed (fresh context)
        - Supports role definition lookup and dynamic role generation

        Args:
            task: SubagentTaskSpec - Spawn task specification
            parent_session: Optional parent session for event forwarding
        """
        if parent_session is None:
            parent_session = get_subagent_parent_session()

        try:
            # 1. Get role definition
            role_def = self._get_role_definition(task.role_id)

            # 2. Determine system_prompt (priority: role def > dynamic generation)
            if role_def and hasattr(role_def, 'system_prompt') and role_def.system_prompt:
                system_prompt = role_def.system_prompt
            else:
                system_prompt = self._generate_dynamic_role_prompt(task.role_id)
                logger.info(f"[SpawnAgent] Generated dynamic role prompt for: {task.role_id}")

            # 3. Create session proxy FIRST (needed for SubagentContextRail to emit events)
            session_proxy: SubagentSessionProxy | None = None
            if parent_session is not None:
                session_proxy = SubagentSessionProxy(
                    parent_session=parent_session,
                    subagent_id=task.task_id,
                    role_id=task.role_id,
                )

            # 4. Create spawn agent (DeepAgent instance)
            spawn_agent = await self._create_spawn_agent(task, system_prompt, parent_session=session_proxy)

            # 5. Build full prompt
            full_prompt = task.objective
            if task.prompt:
                full_prompt = f"{task.objective}\n\n{task.prompt}"

            logger.info(
                f"[SpawnAgent] Starting execution, task_id={task.task_id}, role_id={task.role_id}"
            )

            # 6. Register active agent for permission approval resolution
            self._active_fork_agents[task.task_id] = spawn_agent

            # 7. Set session_id for LLM IO trace logging
            if session_proxy:
                trace_session_id = session_proxy.get_session_id()
            else:
                trace_session_id = task.task_id
            llm_trace_var = _get_llm_trace_session_id_var()
            token_trace_sid = llm_trace_var.set(trace_session_id)

            # 8. Execute with isolated context
            session_id = task.task_id
            invoke_inputs = {"query": full_prompt, "conversation_id": session_id}

            try:
                response = await Runner.run_agent(
                    agent=spawn_agent,
                    inputs=invoke_inputs,
                    session=session_proxy,
                )
            finally:
                self._active_fork_agents.pop(task.task_id, None)
                llm_trace_var.reset(token_trace_sid)

            logger.info(f"[SpawnAgent] Execution completed, task_id={task.task_id}")

            # 9. Extract result and usage
            result_text = ""
            spawn_usage = None
            if isinstance(response, dict):
                result_text = response.get("output", "")
                if isinstance(result_text, dict):
                    result_text = result_text.get("output", str(result_text))
                spawn_usage = response.get("usage")
            elif hasattr(response, "content"):
                result_text = response.content
            elif hasattr(response, "text"):
                result_text = response.text
            else:
                result_text = str(response)

            if spawn_usage:
                logger.info(f"[SpawnAgent] task_id={task.task_id} usage: {spawn_usage}")

            return SubagentResult(
                success=True,
                task_id=task.task_id,
                role_id=task.role_id,
                result=result_text,
                usage=spawn_usage,
            )

        except asyncio.TimeoutError:
            logger.warning(
                f"[SpawnAgent] Timeout after {_DEFAULT_TIMEOUT_SECONDS} seconds, task_id={task.task_id}"
            )
            return SubagentResult(
                success=False,
                task_id=task.task_id,
                role_id=task.role_id,
                error=f"Timeout after {_DEFAULT_TIMEOUT_SECONDS} seconds",
            )
        except Exception as e:
            logger.exception(f"[SpawnAgent] Execution failed: {e}")
            return SubagentResult(
                success=False,
                task_id=task.task_id,
                role_id=task.role_id,
                error=str(e),
            )

    async def _create_spawn_agent(
        self,
        task: SubagentTaskSpec,
        system_prompt: str,
        parent_session: Session | None = None,
    ) -> DeepAgent:
        """Create spawn agent (DeepAgent instance) with isolated context.

        Args:
            task: SubagentTaskSpec
            system_prompt: System prompt for the agent
            parent_session: Parent session for event forwarding

        Returns:
            DeepAgent instance for spawn subagent
        """
        ws, ws_source = self._resolve_subagent_workspace_dir()
        logger.debug(
            "[SpawnAgent] workspace_dir=%s source=%s",
            ws,
            ws_source,
        )

        config_base = get_config()
        language = config_base.get("preferred_language", "zh")

        base_prompt = build_subagent_base_prompt(
            language=language,
            workspace_dir=ws,
            include_time=True,
        )
        # F-REDUCE: Do not append role prompt or use ContextEngineeringRail.
        # Subagent only needs minimal base prompt; tools come from tool schema, not prompt.
        augmented_prompt = base_prompt

        card = AgentCard(
            name=f"spawn_{task.role_id}",
            id=task.task_id,
        )

        workspace_obj = Workspace(
            root_path=ws,
            language=language,
            directories=[],
        )

        max_iterations = self._resolve_subagent_max_iterations()
        filesystem_rail = self._build_inherited_filesystem_rail()
        rails = [
            JiuClawContextEngineeringRail(
                preset=True,
                minimal=True),  # 上下文压缩 - 默认链路 B（ToolResultBudget + MicroCompact + FullCompact），不注入 tools/context
            SubagentContextRail(subagent_id=task.task_id, parent_session=parent_session),
            # active-skill body 的 lift/pin 由 rail.after_tool_call 触发；
            # include_tools/include_skill_body_tools 都关掉：skill_tool/skill_complete
            # 已通过 _inherit_tools_for_spawn 从父 agent 继承，不重复注册。
            # 子类版本跳过 before_model_call 的"# 技能"列表渲染（父 prompt 已指明）。
            SubagentSkillUseRail(
                skills_dir=[str(p) for p in get_agent_registered_skill_dirs()],
                skill_mode=SkillUseRail.SKILL_MODE_ALL,
                include_tools=False,
                include_skill_body_tools=False,
            ),
        ]
        if filesystem_rail is not None:
            rails.insert(0, filesystem_rail)

        spawn_agent = create_deep_agent(
            model=self._model,
            card=card,
            system_prompt=augmented_prompt,
            max_iterations=max_iterations,
            workspace=workspace_obj,
            rails=rails,
            language=language,
            enable_task_loop=False,
            auto_create_workspace=False,
        )

        self._inherit_tools_for_spawn(spawn_agent)

        logger.info(
            "[SpawnAgent] Created spawn agent instance, task_id=%s, max_iterations=%s",
            task.task_id,
            max_iterations,
        )
        return spawn_agent

    def _inherit_tools_for_spawn(self, spawn_agent: DeepAgent) -> None:
        """Inherit tools from parent agent for spawn agent.

        IMPORTANT: Does NOT exclude fork_agent, allowing spawn to call fork.
        """
        try:
            parent_tools = self._parent_agent.ability_manager.list()
            if not parent_tools:
                logger.debug("[SpawnAgent] Parent agent has no tools to inherit")
                return

            inherited_count = 0
            for tool in parent_tools:
                try:
                    tool_name = getattr(tool, "name", None)
                    if hasattr(tool, "card") and hasattr(tool.card, "name"):
                        tool_name = tool.card.name

                    if tool_name in EXCLUDED_TOOLS_SPAWN:
                        logger.debug(f"[SpawnAgent] Skipping excluded tool: {tool_name}")
                        continue

                    if hasattr(tool, "card"):
                        spawn_agent.ability_manager.add(tool.card)
                    else:
                        spawn_agent.ability_manager.add(tool)
                    inherited_count += 1
                except Exception as e:
                    logger.debug(f"[SpawnAgent] Failed to inherit tool: {e}")

            logger.info(
                f"[SpawnAgent] Inherited {inherited_count} tools from parent agent (fork_agent allowed)"
            )
        except Exception as e:
            logger.warning(f"[SpawnAgent] Failed to inherit tools: {e}")

    async def _create_fork_agent(
        self,
        task: ForkAgentTaskSpec,
        fork_messages: list[Any],
        parent_session: Session | None = None,
    ) -> DeepAgent:
        """Create fork agent (DeepAgent instance) with inherited messages.

        Args:
            task: Fork agent task specification
            fork_messages: Messages from parent agent to inherit
            parent_session: Parent session for event forwarding

        Returns:
            DeepAgent instance configured with message injection rail
        """
        ws, ws_source = self._resolve_subagent_workspace_dir()
        logger.info("[ForkAgent] Final workspace_dir=%s, source=%s", ws, ws_source)

        config_base = get_config()
        language = config_base.get("preferred_language", "zh")

        base_prompt = build_subagent_base_prompt(
            language=language,
            workspace_dir=ws,
            include_time=True,
        )

        # Fork agent role prompt — explain inherited context to model
        if language == "cn" or language == "zh":
            role_prompt = f"""---

# Fork 子代理角色

你是一个 AI 助手的 fork 子代理，角色为 `{task.role_id}`。
你继承了父代理的消息历史（上下文），可以访问父代理之前的对话、文档理解和工具调用结果。
使用继承的上下文和可用工具执行给定任务。
"""
        else:
            role_prompt = f"""---

# Fork Subagent Role

You are a fork subagent of an AI assistant, with role `{task.role_id}`.
You inherit parent agent's message history (context), including previous conversations, document understanding, and tool call results.
Execute the given task using inherited context and available tools.
"""

        augmented_prompt = base_prompt + role_prompt

        card = AgentCard(
            name=f"fork_{task.role_id}",
            id=task.task_id,
        )

        workspace_obj = Workspace(
            root_path=ws,
            language=language,
            directories=[],
        )

        max_iterations = self._resolve_subagent_max_iterations()
        filesystem_rail = self._build_inherited_filesystem_rail()
        rails = [
            ForkMessageInjectionRail(fork_messages),  # 注入继承的消息
            JiuClawContextEngineeringRail(
                preset=True,
                minimal=True),  # 上下文压缩 - 默认链路 B，不注入 tools/context
            SubagentContextRail(subagent_id=task.task_id, parent_session=parent_session),
            # 与 spawn 路径同样的 active-skill body lift/pin 接入；
            # fork 继承的 skill_tool/skill_complete 走 _inherit_tools_for_fork。
            SubagentSkillUseRail(
                skills_dir=[str(p) for p in get_agent_registered_skill_dirs()],
                skill_mode=SkillUseRail.SKILL_MODE_ALL,
                include_tools=False,
                include_skill_body_tools=False,
            ),
        ]
        if filesystem_rail is not None:
            rails.insert(0, filesystem_rail)

        fork_agent = create_deep_agent(
            model=self._model,
            card=card,
            system_prompt=augmented_prompt,
            max_iterations=max_iterations,
            workspace=workspace_obj,
            rails=rails,
            language=language,
            enable_task_loop=False,
            auto_create_workspace=False,
        )

        self._inherit_tools_for_fork(fork_agent)

        logger.info(
            "[ForkAgent] Created fork agent instance, task_id=%s, max_iterations=%s",
            task.task_id,
            max_iterations,
        )
        return fork_agent

    def _inherit_tools_for_fork(self, fork_agent: DeepAgent) -> None:
        """Inherit tools from parent agent for fork agent.

        Excludes fork_agent to prevent recursive forking.
        """
        try:
            parent_tools = self._parent_agent.ability_manager.list()
            if not parent_tools:
                logger.debug("[ForkAgent] Parent agent has no tools to inherit")
                return

            inherited_count = 0
            for tool in parent_tools:
                try:
                    tool_name = getattr(tool, "name", None)
                    if hasattr(tool, "card") and hasattr(tool.card, "name"):
                        tool_name = tool.card.name

                    if tool_name in EXCLUDED_TOOLS_FORK:
                        logger.debug(f"[ForkAgent] Skipping excluded tool: {tool_name}")
                        continue

                    if hasattr(tool, "card"):
                        fork_agent.ability_manager.add(tool.card)
                    else:
                        fork_agent.ability_manager.add(tool)
                    inherited_count += 1
                except Exception as e:
                    logger.debug(f"[ForkAgent] Failed to inherit tool: {e}")

            logger.info(f"[ForkAgent] Inherited {inherited_count} tools from parent agent")
        except Exception as e:
            logger.warning(f"[ForkAgent] Failed to inherit tools: {e}")