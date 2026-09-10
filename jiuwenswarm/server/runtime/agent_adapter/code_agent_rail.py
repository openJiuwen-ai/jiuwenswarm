# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CodeAgentRail — 管理 /agents 创建的自定义子智能体。

与 SubagentRail 共存：SubagentRail 管理内置 agent（explore/plan/code/browser），
CodeAgentRail 只管理自定义 agent。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.server.runtime.debug_trace import invoke_subagent_with_trace

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.deep_agent import DeepAgent

_SUB_AGENTS_DIR = "sub_agents"

# ── Tool names that must never be delegated to a sub-agent ──────────────
DISALLOWED_FOR_SUBAGENTS: set[str] = {
    "Agent", "task", "enter_plan_mode", "exit_plan_mode",
    "ask_user_question", "task_stop", "switch_mode",
}

# ── User-facing tool groups for agent-definition UI ────────────────────
TOOL_GROUPS: dict[str, list[str]] = {
    "核心": ["Read", "Write", "Edit", "Bash", "LS"],
    "搜索": ["Grep", "Glob", "WebSearch", "WebFetch"],
    "代码智能": ["LSP", "TodoWrite", "TodoList"],
    "高级": ["MemorySearch", "MemoryGet", "WriteMemory", "EditMemory",
             "CronCreate", "CronList", "CronDelete", "SkillTool"],
    "可视化": ["VisionQA", "ImageOCR", "AudioTranscribe"],
}

# ── Helper: display-name → internal-name mapping ─────────────────────


# Display name → internal SDK name mapping (mirrors openjiuwen.harness.cli.ui.tool_display).
# Defined locally to avoid triggering prompt_toolkit import via cli.ui.__init__ → repl.py.
_DISPLAY_TO_INTERNAL: dict[str, str] = {
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
    "LS": "ls",
    "ListDir": "ls",
    "TodoWrite": "todo_create",
    "TodoList": "todo_list",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "ImageOCR": "image_ocr",
    "VisionQA": "visual_question_answering",
    "AudioTranscribe": "audio_transcription",
    "AudioQA": "audio_question_answering",
    "AudioMetadata": "audio_metadata",
}


def _build_display_to_internal_mapping() -> dict[str, str]:
    """Build reverse mapping: display name → internal name.

    Uses a local mapping instead of importing from openjiuwen.harness.cli.ui.tool_display
    to avoid triggering prompt_toolkit import via cli.ui.__init__ → repl.py.
    """
    return _DISPLAY_TO_INTERNAL


def _filter_tool_cards(
    all_tool_cards: list[ToolCard],
    allowed_tools: list[str],
    disallowed_tools: list[str] | None = None,
) -> list[ToolCard]:
    """Filter ToolCards based on agent definition's tools/disallowed_tools fields.

    Args:
        all_tool_cards: Parent agent's ToolCards (already excluding DISALLOWED_FOR_SUBAGENTS).
        allowed_tools: Agent definition's tools field. ``["*"]`` means all; specific names filter.
        disallowed_tools: Agent definition's disallowed_tools field. ``None`` or ``[]`` means no extra removal.

    Returns:
        Filtered list of ToolCards to pass to sub-agent.
    """
    if allowed_tools == ["*"]:
        result = list(all_tool_cards)
    else:
        display_to_internal = _build_display_to_internal_mapping()
        target_names: set[str] = set()
        for name in allowed_tools:
            # accept both display names ("Read") and internal names ("read_file")
            target_names.add(display_to_internal.get(name, name))
            target_names.add(name)
        result = [tc for tc in all_tool_cards if tc.name in target_names]

    if disallowed_tools:
        display_to_internal = _build_display_to_internal_mapping()
        disallowed_internal: set[str] = set()
        for name in disallowed_tools:
            disallowed_internal.add(display_to_internal.get(name, name))
            disallowed_internal.add(name)
        result = [tc for tc in result if tc.name not in disallowed_internal]

    return result


def _build_agent_tool_card(custom_agents: list, agent_id: str | None = None) -> ToolCard:
    """动态构建 Agent 工具卡片，只列出自定义 agent。"""
    lines = ["Launch a new agent to handle complex, multi-step tasks autonomously."]
    lines.append("")
    lines.append("Available custom agents (created via /agents):")
    for agent_def in custom_agents:
        desc = agent_def.when_to_use or agent_def.description
        tools_desc = ", ".join(agent_def.tools) if agent_def.tools else "*"
        lines.append(f"- {agent_def.name}: {desc} (Tools: {tools_desc})")
    lines.append("")
    lines.append("Usage notes:")
    lines.append("- Each agent starts fresh — provide complete context in the prompt")
    lines.append("- Clearly tell the agent whether you expect it to write code or just to do research")
    lines.append("- Never delegate understanding — write prompts that prove you understood the task")
    lines.append("- Delegate the COMPLETE task, not just the analysis portion")
    lines.append("- Use background=True for independent parallel work")
    lines.append("- You can also invoke agents via @agent-<name> syntax in user messages")

    tool_id = f"agent_tool_{agent_id}" if agent_id else f"agent_tool_{uuid.uuid4().hex}"

    return ToolCard(
        id=tool_id,
        name="Agent",
        description="\n".join(lines),
        input_params={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The name of the custom agent to use",
                },
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus", "haiku"],
                    "description": "Optional model override",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background. You will be notified when complete.",
                    "default": False,
                },
            },
            "required": ["description", "prompt", "subagent_type"],
        },
    )


class AgentTool(Tool):
    """自定义 agent 调度工具。

    直接使用 create_deep_agent() 创建子 agent，不依赖 deep_config.subagents。
    """

    def __init__(self, card: ToolCard, parent_agent: DeepAgent, custom_agents: list):
        super().__init__(card)
        self._parent_agent = parent_agent
        self._custom_agents: dict[str, object] = {a.name: a for a in custom_agents}

    def _build_sub_session_id(self, parent_session_id: str, subagent_type: str) -> str:
        return f"{parent_session_id}_custom_{subagent_type}_{uuid.uuid4().hex[:8]}"

    def _create_sub_agent(self, agent_def, sub_session_id: str) -> DeepAgent:
        """从 AgentDefinition 直接创建子 DeepAgent，绕过 deep_config.subagents。"""
        from openjiuwen.harness.factory import create_deep_agent
        from openjiuwen.harness.schema.config import SubAgentConfig
        from openjiuwen.core.single_agent import AgentCard as OJAgentCard
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _agent_def_to_subagent_config

        parent_config = getattr(self._parent_agent, "deep_config", None)

        # 将 AgentDefinition 转为 SubAgentConfig
        spec = _agent_def_to_subagent_config(
            agent_def,
            parent_config.model,
            parent_config.workspace.root_path if parent_config.workspace else "./",
            getattr(self._parent_agent, "_model_cache", None),
        )

        # 子 agent 工具集：继承父 agent 的 ToolCard，过滤 disallowed 工具
        # AbilityManager.list() 返回 ToolCard/WorkflowCard/AgentCard/McpServerConfig，
        # 不是 Tool 实例，所以直接 isinstance 检查，不要用 getattr(.card)。
        all_tool_cards: list[ToolCard] = []
        if hasattr(self._parent_agent, "ability_manager"):
            for ability in self._parent_agent.ability_manager.list():
                if isinstance(ability, ToolCard) and ability.name not in DISALLOWED_FOR_SUBAGENTS:
                    all_tool_cards.append(ability)

        # 根据 agent 定义的 tools 字段进一步过滤
        # 注意：_agent_def_to_subagent_config() 已将 disallowed_tools 合并进 spec.tools，
        # 所以这里的 disallowed_tools 传 None。
        parent_tool_cards = _filter_tool_cards(
            all_tool_cards,
            allowed_tools=list(spec.tools) if spec.tools else ["*"],
            disallowed_tools=None,
        )

        # 构建 workspace（对齐 DeepAgent.create_subagent 的逻辑）
        parent_workspace_root = (
            str(parent_config.workspace.root_path)
            if parent_config.workspace and isinstance(parent_config.workspace, Workspace)
            else str(parent_config.workspace or ".")
        )
        workspace = Workspace(
            root_path=str(Path(parent_workspace_root) / _SUB_AGENTS_DIR / sub_session_id),
            language=parent_config.language,
        )

        # 构建 create_kwargs（对齐 DeepAgent.create_subagent 的字段映射）
        create_kwargs = {
            "model": spec.model or parent_config.model,
            "card": spec.agent_card,
            "system_prompt": spec.system_prompt,
            "tools": parent_tool_cards,
            "mcps": spec.mcps,
            "enable_task_loop": spec.enable_task_loop,
            "max_iterations": spec.max_iterations if spec.max_iterations is not None else parent_config.max_iterations,
            "workspace": workspace,
            "skills": spec.skills,
            "backend": spec.backend if spec.backend is not None else parent_config.backend,
            # 子 agent 必须复用父 agent 的 SysOperation：它才是用户配置的文件系统边界
            # （本地全量访问 / jiuwenbox 沙箱 / allow-deny 路径）。传 None 会让
            # create_deep_agent 另建一个 LOCAL SysOperation，并按 restrict_to_work_dir
            # 打开 restrict_to_sandbox——本地模式下子 agent 反而被锁进 workspace，沙箱
            # 模式下子 agent 又逃出沙箱直接落到宿主机。
            "sys_operation": getattr(parent_config, "sys_operation", None),
            "language": spec.language if spec.language is not None else parent_config.language,
            "prompt_mode": spec.prompt_mode if spec.prompt_mode is not None else parent_config.prompt_mode,
            "subagents": None,
            "enable_async_subagent": False,
            "add_general_purpose_agent": False,
            "restrict_to_work_dir": spec.restrict_to_work_dir,
        }

        factory_kwargs = dict(spec.factory_kwargs or {})

        sub_agent = create_deep_agent(**create_kwargs, **factory_kwargs)
        logger.info("[AgentTool] Created sub-agent for '%s' via create_deep_agent()", agent_def.name)
        return sub_agent

    async def invoke(self, inputs, **kwargs):
        from openjiuwen.core.session.agent import Session

        parent_session = kwargs.get("session")
        if not isinstance(parent_session, Session):
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason="Agent tool requires a valid session in kwargs",
            )

        if isinstance(inputs, dict):
            subagent_type = inputs.get("subagent_type")
            prompt = inputs.get("prompt")
            background = inputs.get("background", False)
        else:
            subagent_type = getattr(inputs, "subagent_type", None)
            prompt = getattr(inputs, "prompt", None)
            background = getattr(inputs, "background", False)

        if not subagent_type or not prompt:
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason="Both 'subagent_type' and 'prompt' are required",
            )

        agent_def = self._custom_agents.get(subagent_type)
        if agent_def is None:
            available = ", ".join(sorted(self._custom_agents.keys()))
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason=f"Agent type '{subagent_type}' not found. Available custom agents: {available}",
            )

        parent_session_id = parent_session.get_session_id()
        sub_session_id = self._build_sub_session_id(parent_session_id, subagent_type)

        try:
            subagent = self._create_sub_agent(agent_def, sub_session_id)
        except Exception as exc:
            logger.error(f"[AgentTool] Subagent creation failed: type={subagent_type}, error={exc}")
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason=f"Custom agent '{subagent_type}' creation failed: {exc}",
            ) from exc

        if background:
            asyncio.create_task(self._run_async(subagent, prompt, sub_session_id, subagent_type, parent_session))
            return ToolOutput(
                success=True,
                data={
                    "status": "async_launched",
                    "agent_id": subagent_type,
                    "prompt": prompt,
                },
            )
        else:
            try:
                result = await invoke_subagent_with_trace(
                    subagent,
                    inputs={"query": prompt, "conversation_id": sub_session_id},
                    session=parent_session,
                    source_label=f"subagent:custom:{subagent_type}",
                )
                output = result.get("output", "")
                return ToolOutput(
                    success=True,
                    data={"output": output, "agent_id": subagent_type},
                )
            except Exception as exc:
                logger.error(f"[AgentTool] Subagent execution failed: type={subagent_type}, error={exc}")
                raise build_error(
                    StatusCode.TOOL_TASK_TOOL_INVOKED,
                    reason=f"Custom agent '{subagent_type}' execution failed: {exc}",
                ) from exc

    async def _run_async(
        self, subagent: DeepAgent, prompt: str, sub_session_id: str,
        subagent_type: str, parent_session: Session,
    ) -> None:
        try:
            await invoke_subagent_with_trace(
                subagent,
                inputs={"query": prompt, "conversation_id": sub_session_id},
                session=parent_session,
                source_label=f"subagent:custom:{subagent_type}",
            )
        except Exception as exc:
            logger.error(
                "[AgentTool] Async subagent '%s' failed: %s", subagent_type, exc
            )

    async def stream(self, inputs, **kwargs):
        pass


class CodeAgentRail(DeepAgentRail):
    """Code 模式下的自定义 agent rail。

    与 SubagentRail 共存，只管理 /agents 创建的自定义 agent。
    不触碰内置 agent（explore/plan/code/browser）。
    """

    priority = 90  # 略低于 SubagentRail(95)，避免冲突

    def __init__(self, workspace_dir: str):
        super().__init__()
        self._workspace_dir = workspace_dir
        self._agent: DeepAgent | None = None
        self._agent_tool: AgentTool | None = None

    def init(self, agent: DeepAgent) -> None:
        self._agent = agent
        self._register_agent_tool()

    def uninit(self, agent: DeepAgent) -> None:
        self._unregister_agent_tool(agent)
        self._agent = None

    def set_workspace_dir(self, workspace_dir: str) -> None:
        """Rebind custom-agent discovery to the current Code workspace."""
        normalized = str(workspace_dir or "").strip()
        if not normalized or normalized == self._workspace_dir:
            return
        self._workspace_dir = normalized
        if self._agent is not None:
            self._unregister_agent_tool(self._agent)
            self._register_agent_tool()

    def _register_agent_tool(self) -> None:
        custom_agents = self._load_custom_agents()
        if not custom_agents:
            logger.info("[CodeAgentRail] No custom agents found, Agent tool not registered")
            return

        agent_id = getattr(getattr(self._agent, "card", None), "id", None)
        card = _build_agent_tool_card(custom_agents, agent_id)
        self._agent_tool = AgentTool(
            card=card,
            parent_agent=self._agent,
            custom_agents=custom_agents,
        )
        # Unified registration: add_ability qualifies the stateful tool id and
        # binds it in the resource manager, so teardown_tools drops it at
        # round-end instead of leaking a bare id that refresh-warns on rebuild.
        self._agent.ability_manager.add_ability(self._agent_tool.card, self._agent_tool)
        logger.info(
            "[CodeAgentRail] Agent tool registered with %d custom agent(s): %s",
            len(custom_agents),
            ", ".join(a.name for a in custom_agents),
        )

    def _unregister_agent_tool(self, agent: DeepAgent) -> None:
        if self._agent_tool is None:
            return
        if hasattr(agent, "ability_manager"):
            name = getattr(self._agent_tool.card, "name", None)
            if name:
                try:
                    # Mirror add_ability: removes the agent-qualified id from
                    # both this manager and the shared resource manager.
                    agent.ability_manager.remove_ability(name)
                except Exception as e:
                    logger.debug("Failed to remove agent tool '%s': %s", name, e)
        self._agent_tool = None

    def _load_custom_agents(self) -> list:
        """从 AgentConfigService 加载启用的自定义 agent。"""
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            service = AgentConfigService(self._workspace_dir)
            return [
                a for a in service.list_agents()
                if a.source != "builtin" and a.enabled == True
            ]
        except Exception:
            logger.warning("[CodeAgentRail] Failed to load custom agents", exc_info=True)
            return []


__all__ = ["CodeAgentRail", "AgentTool", "DISALLOWED_FOR_SUBAGENTS", "TOOL_GROUPS", "_filter_tool_cards"]
