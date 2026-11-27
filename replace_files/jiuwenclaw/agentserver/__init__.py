# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer 模块."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenclaw.agentserver.interface import JiuWenClaw
    from jiuwenclaw.agentserver.skill_manager import SkillManager

# 导入注册模型客户端
from jiuwenclaw.agentserver.skilldev.llm import xiaoyi_websocket
__all__ = ["JiuWenClaw", "SkillManager"]


def __getattr__(name: str) -> Any:
    if name == "JiuWenClaw":
        from jiuwenclaw.agentserver.interface import JiuWenClaw

        return JiuWenClaw
    if name == "SkillManager":
        from jiuwenclaw.agentserver.skill_manager import SkillManager

        return SkillManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
