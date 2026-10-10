# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""失败归因诊断 Rail：采集轨迹并在任务失败时做步级归因。"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from openjiuwen.agent_evolving.trajectory import Trajectory
from openjiuwen.harness.rails.evolution.trajectory_rail import TrajectoryRail

from jiuwenswarm.agents.harness.common.diagnosis.attribution import (
    AttributionResult,
    StepAttributor,
)
from jiuwenswarm.agents.harness.common.diagnosis.trajectory_view import build_step_view


class FailureAttributionRail(TrajectoryRail):
    """采集轨迹并在任务失败时做步级归因诊断。

    不覆盖 ``after_invoke`` 等生命周期钩子，避免破坏父类 EvolutionRail
    的轨迹采集；调用方在判定失败后显式调用 ``diagnose``。

    默认 ``kinds=("tool", "llm")``，步级视图同时包含工具步与 LLM 步。
    若只需工具步（如论文实验脚本），须显式传入 ``kinds=("tool",)``。
    """

    def __init__(
        self,
        attributor: Optional[StepAttributor] = None,
        *,
        trajectory_store: Any = None,
        failure_detector: Optional[Callable[[str], bool]] = None,
        on_result: Optional[Callable[[AttributionResult], None]] = None,
        kinds: Sequence[str] = ("tool", "llm"),
    ) -> None:
        super().__init__(trajectory_store=trajectory_store)
        self._attributor = attributor
        self._failure_detector = failure_detector
        self._on_result = on_result
        self._kinds: Sequence[str] = tuple(kinds)
        self._last_result: Optional[AttributionResult] = None

    @property
    def last_result(self) -> Optional[AttributionResult]:
        """最近一次诊断结果。"""
        return self._last_result

    def _build_trajectory(self, ctx=None, *, finalize: bool = False):
        """强制走 builder 分支落盘轨迹。

        父类 ``_build_trajectory`` 在 trace 侧有 span 时优先走 OTLP，
        而 ReActAgent 不产生 llm span，默认落盘会丢掉全部 LLM 步。
        ``ctx`` 在父类里只用于选择 OTLP 分支，传 ``None`` 即可落到
        builder，并保留父类对 meta/source/session_id/cost 的处理。
        """
        return super()._build_trajectory(None, finalize=finalize)

    async def diagnose(
        self,
        trajectory: Trajectory,
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
        kinds: Optional[Sequence[str]] = None,
    ) -> Optional[AttributionResult]:
        """构造步级视图并调用 attributor；无 attributor 时返回 None。

        默认同时包含 tool 与 llm 步。仅要工具步时须传入
        ``kinds=("tool",)``；未传时沿用实例构造时的默认值。
        """
        if self._attributor is None:
            return None
        if self._failure_detector is not None and not self._failure_detector(
            final_answer
        ):
            return None
        used = self._kinds if kinds is None else kinds
        steps = build_step_view(trajectory, kinds=used)
        result = await self._attributor.attribute(
            steps,
            task=task,
            final_answer=final_answer,
            expected_answer=expected_answer,
        )
        self._last_result = result
        if self._on_result is not None:
            self._on_result(result)
        return result
