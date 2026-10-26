# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer 模块."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

__all__ = ["JiuWenSwarm", "SkillManager"]


def __getattr__(name: str) -> Any:
    if name == "JiuWenSwarm":
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        return JiuWenSwarm
    if name == "SkillManager":
        from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

        return SkillManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
