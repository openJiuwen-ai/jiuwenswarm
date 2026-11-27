# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDev — Skill 开发模式模块.

提供 Skill 创建/优化/升级的全流程能力，面向平台前端开发者。

主要入口：
    SkillDevService.handle(request) → AsyncIterator[AgentResponseChunk]

核心组件：
    schema.py       — 数据模型（阶段、状态、事件、挂起点）
    deps.py         — 最小外部依赖定义（由 JiuWenClaw 注入）
    store.py        — 状态持久化（StateStore）
    workspace.py    — 工作区管理（WorkspaceProvider）
    context.py      — 阶段执行上下文（SkillDevContext）
    pipeline.py     — 确定性状态机编排器（SkillDevPipeline）
    service.py      — 无状态请求处理器（SkillDevService）
    stages/         — 各阶段处理器（StageHandler 子类）
"""

from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.service import SkillDevService
from jiuwenclaw.agentserver.skilldev.store import StateStore
from jiuwenclaw.agentserver.skilldev.workspace import WorkspaceProvider

__all__ = [
    "SkillDevDeps",
    "SkillDevService",
    "StateStore",
    "WorkspaceProvider",
]
