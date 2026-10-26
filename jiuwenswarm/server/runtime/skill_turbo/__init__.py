# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 模块。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExecutionTrace": ".evolver",
    "PlanCodeLoadError": ".executor",
    "PlanCodeValidationError": ".validator",
    "PlanCodeValidator": ".validator",
    "PlanExecution": ".evolver",
    "PlanGenerationError": ".planner",
    "PlanNode": ".plan_node",
    "SkillTurbo": ".agent",
    "SkillTurboEnvironment": ".environment",
    "SkillTurboEvolver": ".evolver",
    "SkillTurboExecutor": ".executor",
    "SkillTurboPlanner": ".planner",
    "Skill": ".environment",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}{_EXPORTS[name]}")
    value = getattr(module, name)
    globals()[name] = value
    return value
