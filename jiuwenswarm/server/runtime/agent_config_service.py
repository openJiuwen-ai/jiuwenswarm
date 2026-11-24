# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 配置管理服务 — 管理内置和自定义 agent 定义的 CRUD 操作.

Agent 定义来源（优先级从高到低）：
- project: <workspace>/.jiuwenswarm/agents/*.md
- user:    ~/.jiuwenswarm/agents/*.md
- local:   <workspace>/.jiuwenswarm/agents-local/*.md
- builtin: 代码内置

文件格式为 YAML frontmatter + Markdown body。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    STATUSLINE_SETUP_AGENT_DESCRIPTION,
    STATUSLINE_SETUP_AGENT_TYPE,
    STATUSLINE_SETUP_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


_TOOL_DESCRIPTIONS: dict[str, str] = {
    "Read": "读取文件内容",
    "Write": "写入文件",
    "Edit": "编辑文件（精准替换）",
    "Bash": "执行 shell 命令",
    "LS": "列出目录内容",
    "Grep": "搜索文件内容",
    "Glob": "按模式搜索文件名",
    "WebSearch": "网络搜索",
    "WebFetch": "获取网页内容",
    "LSP": "代码智能（定义跳转、引用查找）",
    "TodoWrite": "创建/更新任务列表",
    "TodoList": "查看任务列表",
    "MemorySearch": "搜索记忆",
    "MemoryGet": "获取记忆条目",
    "WriteMemory": "写入记忆",
    "EditMemory": "编辑记忆",
    "CronCreate": "创建定时任务",
    "CronList": "列出定时任务",
    "CronDelete": "删除定时任务",
    "SkillTool": "调用 Skill",
    "VisionQA": "视觉问答",
    "ImageOCR": "图片文字识别",
    "AudioTranscribe": "音频转录",
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


AgentSource = Literal["builtin", "user", "project", "local"]


@dataclass
class AgentDefinition:
    """Agent 定义数据模型。"""

    name: str
    description: str
    prompt: str
    source: AgentSource
    file_path: str | None = None
    model: str | None = None
    tools: list[str] = field(default_factory=lambda: ["*"])
    disallowed_tools: list[str] = field(default_factory=list)
    color: str | None = None
    permission_mode: str | None = None
    memory_scope: str | None = None
    shadowed_by: AgentSource | None = None
    enabled: bool | None = None  # None = 不在 config.yaml 中（内置 agent 默认 None）
    when_to_use: str | None = None  # 告诉 LLM 何时调度此 agent
    max_iterations: int | None = None  # 子 agent 最大迭代次数（openjiuwen SubAgentConfig.max_iterations）
    skills: list[str] | None = None  # 子 agent 启动时预加载的 skill 名称


@dataclass
class CreateAgentParams:
    """创建 agent 的请求参数。"""

    name: str
    description: str
    prompt: str
    location: AgentSource
    model: str | None = None
    tools: list[str] | None = None
    color: str | None = None
    permission_mode: str | None = None
    memory_scope: str | None = None
    disallowed_tools: list[str] | None = None
    when_to_use: str | None = None
    max_iterations: int | None = None
    skills: list[str] | None = None


@dataclass
class UpdateAgentParams:
    """更新 agent 的请求参数（所有字段可选，None 表示不修改）。"""

    description: str | None = None
    when_to_use: str | None = None
    prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    color: str | None = None
    permission_mode: str | None = None
    memory_scope: str | None = None
    disallowed_tools: list[str] | None = None
    max_iterations: int | None = None
    skills: list[str] | None = None


# ---------------------------------------------------------------------------
# 内置 Agent 定义
# ---------------------------------------------------------------------------

BUILTIN_AGENTS: list[AgentDefinition] = [
    AgentDefinition(
        name=STATUSLINE_SETUP_AGENT_TYPE,
        description=STATUSLINE_SETUP_AGENT_DESCRIPTION,
        prompt=STATUSLINE_SETUP_SYSTEM_PROMPT,
        source="builtin",
        tools=["Read", "Write", "Edit", "Bash"],
        enabled=True,
        max_iterations=DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    ),
    AgentDefinition(
        name="general-purpose",
        description="通用多步任务 agent，适用于没有专用 agent 的各类任务",
        prompt=(
            "你是一个通用任务 agent。使用可用工具完成用户委派的任务。\n\n"
            "工作原则：\n"
            "1. 将复杂任务分解为可管理的步骤\n"
            "2. 在每个步骤完成后汇报进展\n"
            "3. 遇到阻塞时主动说明需要什么信息"
        ),
        source="builtin",
        tools=["*"],
    ),
    AgentDefinition(
        name="Explore",
        description="快速只读代码库探索 agent，用于定位代码、搜索符号、查找文件",
        prompt=(
            "你是代码库探索专家。你的职责是快速定位代码、搜索符号和查找文件。\n\n"
            "工作原则：\n"
            "1. 只进行只读操作（搜索、读取、列出文件）\n"
            "2. 通过多种搜索策略（文件名模式、grep 符号、目录遍历）确保覆盖全面\n"
            "3. 返回精确的文件路径和行号\n"
            "4. 当结果过多时，缩小搜索范围而不是截断输出"
        ),
        source="builtin",
        tools=["Read", "Bash", "Grep", "Glob"],
    ),
    AgentDefinition(
        name="Plan",
        description="软件架构设计 agent，用于规划实现方案",
        prompt=(
            "你是软件架构师。分析代码库模式和约定，提供完整的实现蓝图。\n\n"
            "工作原则：\n"
            "1. 先理解现有代码库的架构模式和约定\n"
            "2. 设计变更时考虑副作用和依赖关系\n"
            "3. 输出包含：需要创建/修改的文件、组件设计、数据流和构建顺序\n"
            "4. 不写实现代码，只提供设计蓝图"
        ),
        source="builtin",
        tools=["Read", "Bash", "Grep", "Glob"],
    ),
]

_SOURCE_SORT_ORDER: dict[str, int] = {"builtin": 0, "local": 1, "user": 2, "project": 3}


def _source_sort_key(agent: AgentDefinition) -> int:
    return _SOURCE_SORT_ORDER.get(agent.source, 99)


# ---------------------------------------------------------------------------
# AgentConfigService
# ---------------------------------------------------------------------------


class AgentConfigService:
    """管理 agent 定义的 CRUD 操作。

    支持四个来源的 agent 定义：内置、用户级、项目级、本地级。
    同名 agent 按 project > user > local > builtin 优先级覆盖。
    """

    def __init__(self, workspace_dir: Path | str | None = None):
        self._workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()

    # ---- 路径 ----

    @staticmethod
    def _get_user_agents_dir() -> Path:
        return get_user_workspace_dir() / "agents"

    def _get_project_agents_dir(self) -> Path:
        return self._workspace_dir / ".jiuwenswarm" / "agents"

    def _get_local_agents_dir(self) -> Path:
        return self._workspace_dir / ".jiuwenswarm" / "agents-local"

    # ---- CRUD ----

    def list_agents(self) -> list[AgentDefinition]:
        """列出所有 agent（内置 + 自定义），按优先级合并。

        加载顺序决定优先级：后加载的覆盖先加载的，因此
        project > user > local > builtin。被覆盖的同名 agent 标记 shadowed_by。
        同时从 config.yaml 的 react.subagents 读取 enabled 状态。
        """
        sources: list[tuple[list[AgentDefinition], AgentSource]] = [
            (list(BUILTIN_AGENTS), "builtin"),
            (self._load_from_dir(self._get_local_agents_dir(), "local"), "local"),
            (self._load_from_dir(self._get_user_agents_dir(), "user"), "user"),
            (self._load_from_dir(self._get_project_agents_dir(), "project"), "project"),
        ]

        # 读取 config.yaml 中的 react.subagents enabled 状态
        subagent_states: dict[str, bool] = {}
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
            react = config.get("react") if isinstance(config, dict) else None
            subagents_cfg = react.get("subagents") if isinstance(react, dict) else None
            if isinstance(subagents_cfg, dict):
                for name, cfg in subagents_cfg.items():
                    if isinstance(cfg, dict) and "enabled" in cfg:
                        subagent_states[name] = bool(cfg["enabled"])
        except Exception as e:
            logger.debug("Failed to load subagent states from config: %s", e)

        # 按名字分组，保持所有来源的 agent（包括被 shadow 的）
        grouped: dict[str, list[AgentDefinition]] = {}
        for agents, _ in sources:
            for agent in agents:
                grouped.setdefault(agent.name, []).append(agent)

        # 每组的最后一个为 active（最高优先级），之前的标记 shadowed_by
        result: list[AgentDefinition] = []
        for _name, group in grouped.items():
            active = group[-1]
            active.shadowed_by = None
            for agent in group[:-1]:
                agent.shadowed_by = active.source
                result.append(agent)
            result.append(active)

        # 注入 enabled 状态
        for agent in result:
            if agent.name in subagent_states:
                agent.enabled = subagent_states[agent.name]

        return sorted(result, key=_source_sort_key)

    def get_agent(self, name: str) -> AgentDefinition | None:
        """获取单个 agent 完整定义（含 system prompt 正文）。

        返回活跃版本（未被 shadow 的），与 list_agents 保持一致的优先级语义。
        """
        agents = self.list_agents()
        for a in agents:
            if a.name == name and a.shadowed_by is None:
                return a
        return None

    def create_agent(self, params: CreateAgentParams) -> AgentDefinition:
        """创建新的自定义 agent，写入 markdown 文件。

        Raises:
            ValueError: 同名内置 agent 已存在时，或名称格式不符合要求
        """
        import re
        name = params.name.strip()
        if not re.match(r'^[a-zA-Z0-9_-]{3,50}$', name):
            raise ValueError(
                f"Agent 名称格式无效: '{name}'。要求 3-50 字符，仅允许字母、数字、连字符、下划线"
            )

        existing = self.get_agent(name)
        if existing is not None and existing.source == "builtin":
            raise ValueError(f"不能覆盖内置 agent: {name}")

        target_dir = self._resolve_location_dir(params.location)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{name}.md"

        params.name = name
        content = _format_agent_file(params)
        file_path.write_text(content, encoding="utf-8")

        logger.info("Created agent '%s' at %s", name, file_path)

        return AgentDefinition(
            name=name,
            description=params.description,
            prompt=params.prompt,
            source=params.location,
            file_path=str(file_path),
            model=params.model,
            tools=params.tools or ["*"],
            color=params.color,
            permission_mode=params.permission_mode,
            memory_scope=params.memory_scope,
            max_iterations=params.max_iterations,
            skills=params.skills,
        )

    def update_agent(self, name: str, params: UpdateAgentParams) -> AgentDefinition:
        """更新自定义 agent 定义，覆盖写入文件。

        Raises:
            ValueError: agent 不存在或为内置 agent
        """
        name = name.strip()
        agent = self.get_agent(name)
        if agent is None:
            raise ValueError(f"Agent 不存在: {name}")
        if agent.source == "builtin":
            raise ValueError(f"不能修改内置 agent: {name}")
        if not agent.file_path:
            raise ValueError(f"Agent 无文件路径: {name}")

        _apply_update_params(agent, params)

        content = _format_agent_file(agent)
        Path(agent.file_path).write_text(content, encoding="utf-8")

        logger.info("Updated agent '%s' at %s", name, agent.file_path)
        return agent

    def delete_agent(self, name: str) -> bool:
        """删除自定义 agent 定义文件。

        Raises:
            ValueError: agent 为内置 agent
        """
        name = name.strip()
        agent = self.get_agent(name)
        if agent is None:
            return False
        if agent.source == "builtin":
            raise ValueError(f"不能删除内置 agent: {name}")
        if agent.file_path:
            p = Path(agent.file_path)
            if p.exists():
                p.unlink()
                logger.info("Deleted agent '%s' at %s", name, agent.file_path)
            return True
        return False

    @staticmethod
    def list_available_tools() -> dict:
        """Return available tools with display names, internal names, descriptions, and groups."""
        from jiuwenswarm.server.runtime.agent_adapter.code_agent_rail import TOOL_GROUPS, DISALLOWED_FOR_SUBAGENTS
        from openjiuwen.harness.cli.ui.tool_display import _TOOL_DISPLAY_NAMES

        # Build internal → display mapping (deduplicated)
        internal_to_display: dict[str, str] = {}
        for internal_name, display_name in _TOOL_DISPLAY_NAMES.items():
            if internal_name not in internal_to_display:
                internal_to_display[internal_name] = display_name

        # Build display → group mapping from TOOL_GROUPS
        display_to_group: dict[str, str] = {}
        for group_name, display_names in TOOL_GROUPS.items():
            for dn in display_names:
                display_to_group[dn] = group_name

        # Build tool list from known internal names (deduplicated)
        tools = []
        seen_display = set()
        for internal_name, display_name in internal_to_display.items():
            if display_name in seen_display:
                continue
            seen_display.add(display_name)
            group = display_to_group.get(display_name, "高级")
            description = _TOOL_DESCRIPTIONS.get(display_name, display_name)
            tools.append({
                "name": display_name,
                "internal_name": internal_name,
                "description": description,
                "group": group,
            })

        # Add tools referenced in TOOL_GROUPS but not in _TOOL_DISPLAY_NAMES
        # (e.g., "LSP" whose internal name is "lsp")
        for group_name, display_names in TOOL_GROUPS.items():
            for dn in display_names:
                if dn not in seen_display:
                    seen_display.add(dn)
                    tools.append({
                        "name": dn,
                        "internal_name": dn.lower(),
                        "description": _TOOL_DESCRIPTIONS.get(dn, dn),
                        "group": group_name,
                    })

        return {
            "tools": tools,
            "groups": list(TOOL_GROUPS.keys()),
            "disallowed_for_subagents": list(DISALLOWED_FOR_SUBAGENTS),
        }

    # ---- 内部方法 ----

    def _resolve_location_dir(self, location: str) -> Path:
        mapping = {
            "user": self._get_user_agents_dir(),
            "project": self._get_project_agents_dir(),
            "local": self._get_local_agents_dir(),
        }
        if location not in mapping:
            raise ValueError(f"无效的 location: {location}，有效值: user, project, local")
        return mapping[location]

    @staticmethod
    def _load_from_dir(dir_path: Path, source: AgentSource) -> list[AgentDefinition]:
        """从目录加载所有 .md agent 定义文件。"""
        if not dir_path.exists():
            return []
        agents: list[AgentDefinition] = []
        for md_file in sorted(dir_path.glob("*.md")):
            try:
                agent = _parse_agent_file(md_file, source)
                if agent is not None:
                    agents.append(agent)
            except Exception:
                logger.warning("Failed to parse agent file: %s", md_file, exc_info=True)
        return agents


# ---------------------------------------------------------------------------
# 文件解析 / 生成
# ---------------------------------------------------------------------------


def _parse_agent_file(file_path: Path, source: AgentSource) -> AgentDefinition | None:
    """解析 YAML frontmatter + Markdown body 格式的 agent 文件。"""
    content = file_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = yaml.safe_load(parts[1])
    prompt = parts[2].strip()
    if not frontmatter or "name" not in frontmatter:
        return None
    return AgentDefinition(
        name=frontmatter["name"],
        description=frontmatter.get("description", ""),
        when_to_use=frontmatter.get("when_to_use"),
        prompt=prompt,
        source=source,
        file_path=str(file_path),
        model=frontmatter.get("model"),
        tools=frontmatter.get("tools", ["*"]),
        disallowed_tools=frontmatter.get("disallowed_tools", []),
        color=frontmatter.get("color"),
        permission_mode=frontmatter.get("permission_mode"),
        memory_scope=frontmatter.get("memory_scope"),
        max_iterations=frontmatter.get("max_iterations"),
        skills=frontmatter.get("skills"),
    )


def _format_agent_file(params: CreateAgentParams | AgentDefinition) -> str:
    """生成 YAML frontmatter + Markdown body 格式的 agent 文件内容。"""
    frontmatter: dict = {
        "name": params.name,
        "description": params.description,
    }
    prompt: str = params.prompt if hasattr(params, "prompt") else ""

    if hasattr(params, "when_to_use") and params.when_to_use:
        frontmatter["when_to_use"] = params.when_to_use
    if params.model:
        frontmatter["model"] = params.model
    if params.tools and params.tools != ["*"]:
        frontmatter["tools"] = params.tools
    if hasattr(params, "color") and params.color:
        frontmatter["color"] = params.color
    if hasattr(params, "permission_mode") and params.permission_mode:
        frontmatter["permission_mode"] = params.permission_mode
    if hasattr(params, "memory_scope") and params.memory_scope:
        frontmatter["memory_scope"] = params.memory_scope

    if hasattr(params, "disallowed_tools") and params.disallowed_tools:
        frontmatter["disallowed_tools"] = params.disallowed_tools
    if hasattr(params, "max_iterations") and params.max_iterations is not None:
        frontmatter["max_iterations"] = params.max_iterations
    if hasattr(params, "skills") and params.skills:
        frontmatter["skills"] = params.skills

    yaml_str = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False).strip()
    return f"---\n{yaml_str}\n---\n\n{prompt}\n"


def _apply_update_params(agent: AgentDefinition, params: UpdateAgentParams) -> None:
    """将 UpdateAgentParams 的非 None 字段应用到 AgentDefinition。"""
    if params.description is not None:
        agent.description = params.description
    if params.when_to_use is not None:
        agent.when_to_use = params.when_to_use
    if params.prompt is not None:
        agent.prompt = params.prompt
    if params.model is not None:
        agent.model = params.model
    if params.tools is not None:
        agent.tools = params.tools
    if params.color is not None:
        agent.color = params.color
    if params.permission_mode is not None:
        agent.permission_mode = params.permission_mode
    if params.memory_scope is not None:
        agent.memory_scope = params.memory_scope
    if params.disallowed_tools is not None:
        agent.disallowed_tools = params.disallowed_tools
    if params.max_iterations is not None:
        agent.max_iterations = params.max_iterations
    if params.skills is not None:
        agent.skills = params.skills
