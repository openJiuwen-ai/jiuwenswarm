# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevDeps — SkillDevService 的最小外部依赖定义.

设计原则：SkillDevService 不依赖 JiuWenSwarm 实例，
只接收以下最小依赖集，由 JiuWenSwarm 在初始化时注入。

JiuWenSwarm 内部的 SkillManager、EvolutionService、对话历史等
对 SkillDev 完全不可见，确保模块边界清晰。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jiuwenswarm.server.runtime.skill.skilldev.store import StateStore
from jiuwenswarm.server.runtime.skill.skilldev.workspace import WorkspaceProvider


@dataclass
class SkillDevDeps:
    """SkillDevService 的全部外部依赖（由 JiuWenSwarm 构造并注入）."""

    # 模型配置：为每个阶段创建独立 ReActAgent 的基础
    model_name: str
    model_client_config: dict

    # 工具能力：按需给 Agent 配工具
    # mcp_tools_factory: 返回当前可用 MCP 工具列表的工厂函数
    mcp_tools_factory: Callable[[], list]
    # sysop_config: 文件系统访问配置（SysOperationCard）；None 表示禁止文件操作
    sysop_config: object | None

    # 基础设施
    state_store: StateStore
    workspace_provider: WorkspaceProvider
