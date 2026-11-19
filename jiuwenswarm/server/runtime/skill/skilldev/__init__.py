# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDev — Skill 开发模式模块.

提供 Skill 创建/优化/升级的全流程能力，面向平台前端开发者。

主要入口：
    SkillDevService.handle(request) → AsyncIterator[AgentResponseChunk]

核心组件：
    schema.py       — 数据模型（阶段、状态、事件、挂起点）
    deps.py         — 最小外部依赖定义（由 JiuWenSwarm 注入）
    store.py        — 状态持久化（StateStore）
    workspace.py    — 工作区管理（WorkspaceProvider）
    context.py      — 阶段执行上下文（SkillDevContext）
    pipeline.py     — 确定性状态机编排器（SkillDevPipeline）
    service.py      — 无状态请求处理器（SkillDevService）
    stages/         — 各阶段处理器（StageHandler 子类）
"""

import importlib
from types import ModuleType

from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
    filter_visible_skill_names,
    get_registered_skill_names,
    get_skill_enabled,
    get_state_file,
    list_disabled_skills,
    list_execution_disabled_skills,
    load_execution_disabled_skills,
    normalize_local_skills,
    normalize_skill_configs,
    set_skill_enabled,
)

# Loaded on attribute access. The package is also imported for skill
# enablement helpers; importing the request handler at module import time
# would pull it into that path.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "SkillDevDeps": (
        "jiuwenswarm.server.runtime.skill.skilldev.deps",
        "SkillDevDeps",
    ),
    "SkillDevService": (
        "jiuwenswarm.server.runtime.skill.skilldev.service",
        "SkillDevService",
    ),
    "StateStore": (
        "jiuwenswarm.server.runtime.skill.skilldev.store",
        "StateStore",
    ),
    "WorkspaceProvider": (
        "jiuwenswarm.server.runtime.skill.skilldev.workspace",
        "WorkspaceProvider",
    ),
}


def __getattr__(name: str) -> object:
    """Resolve service types when a skilldev request imports them."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module: ModuleType = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = [
    "SkillDevDeps",
    "SkillDevService",
    "StateStore",
    "WorkspaceProvider",
    "filter_visible_skill_names",
    "get_registered_skill_names",
    "get_skill_enabled",
    "get_state_file",
    "list_disabled_skills",
    "list_execution_disabled_skills",
    "load_execution_disabled_skills",
    "normalize_local_skills",
    "normalize_skill_configs",
    "set_skill_enabled",
]
