# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""StageHandler 基类和 StageResult.

每个阶段处理器继承 StageHandler，实现 execute() 方法。
execute() 执行完成后返回 StageResult，告知 Pipeline 下一个跳转阶段。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from jiuwenclaw.agentserver.skilldev.schema import SkillDevStage


@dataclass
class StageResult:
    """阶段执行结果，由 Pipeline 读取以驱动状态跳转."""

    next_stage: SkillDevStage


class StageHandler(ABC):
    """SkillDev Pipeline 阶段处理器基类.

    每个阶段独立实现，通过 execute() 与 Pipeline 交互。
    处理器不应持有跨请求的状态——所有状态均通过 SkillDevContext 传入。
    """

    @abstractmethod
    async def execute(self, ctx) -> StageResult:
        """执行阶段逻辑.

        Args:
            ctx: SkillDevContext，包含 state、workspace、emit、create_stage_agent 等

        Returns:
            StageResult，Pipeline 据此跳转到下一阶段
        """
