# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevDeps — SkillDevService 的最小外部依赖定义.

设计原则：SkillDevService 不依赖 JiuWenClaw 实例，
只接收以下最小依赖集，由 JiuWenClaw 在初始化时注入。

JiuWenClaw 内部的 SkillManager、EvolutionService、对话历史等
对 SkillDev 完全不可见，确保模块边界清晰。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from jiuwenclaw.agentserver.skilldev.store import StateStore
from jiuwenclaw.agentserver.skilldev.workspace import WorkspaceProvider


@dataclass
class SkillDevDeps:
    """SkillDevService 的全部外部依赖（由 JiuWenClaw 构造并注入）."""

    # 模型配置：为每个阶段创建独立 ReActAgent 的基础
    model_name: str
    model_client_config: dict
    model_config_obj: dict

    # sysop_config: 文件系统访问配置（SysOperationCard）；None 表示禁止文件操作
    sysop_config: object | None

    # 基础设施
    state_store: StateStore
    workspace_provider: WorkspaceProvider
    session_history: Any | None = None

    # 取消信号：task_id → asyncio.Event；由 _handle_start/_handle_respond 注册，
    # _handle_cancel 通过 event.set() 通知 pipeline 在下一阶段边界终止。
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
