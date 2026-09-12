# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""程序化局部验证归因器：按见证窗口扫描轨迹，不调用 LLM。

本模块只提供通用骨架。具体领域的步检查逻辑（例如算术链表达式重算、
题面一致性）由调用方通过依赖注入提供，以便脱离单一实验仍可复用。

弃权（``step_index=None``）是一等公民：当给定窗口下找不到任何失败证据时
必须弃权，不得猜测。弃权本身即表明该轨迹在对应窗口下不可局部验证。
"""

from __future__ import annotations

from typing import Callable, Literal, Optional, Union

from jiuwenswarm.agents.harness.common.diagnosis.attribution import (
    AttributionResult,
    StepAttributor,
)
from jiuwenswarm.agents.harness.common.diagnosis.trajectory_view import StepView

Window = Union[int, Literal["spec"]]
CheckKind = Literal["local", "link", "spec"]

# 步检查器：失败返回原因字符串，通过返回 None。
# kind 取值：
#   - local：单步自洽（声明的表达式与结果是否一致）
#   - link ：跨步链接（上一步结果是否等于本步首操作数；第一步可对照题面初值）
#   - spec ：题面一致性（本步操作/操作数是否与题面第 k 条指令一致）
StepChecker = Callable[..., Optional[str]]


def _window_name(window: Window) -> str:
    if window == "spec":
        return "spec"
    return str(window)


def _empty_result(attributor: str) -> AttributionResult:
    return AttributionResult(
        attributor=attributor,
        step_index=None,
        total_steps=0,
        rationale="",
        error="empty_steps",
    )


class ProgrammaticAttributor(StepAttributor):
    """不使用 LLM 的程序化局部验证归因器。

    按 ``k = 1..L`` 顺序扫描；对每一步按见证窗口执行检查，返回第一个
    失败步的 1-based 索引。全部通过则弃权（``step_index=None``）。

    窗口语义
    --------
    - ``window=1``：只做单步自洽检查（local）
    - ``window=2``：local + 跨步链接检查（link）
    - ``window="spec"``：local + link + 题面一致性检查（spec）

    Parameters
    ----------
    checker:
        可调用对象，签名建议为
        ``checker(step, *, prev, task, kind, tolerance) -> Optional[str]``。
        返回非空字符串表示该检查失败及原因；返回 ``None`` 表示通过。
    window:
        见证窗口，见上。
    tolerance:
        传给检查器的容差（例如有效数字舍入下的 ε）；框架本身不做数值解释。
    name:
        可选覆盖 ``attributor`` 名称；默认 ``programmatic_w{window}``。
    """

    name = "programmatic"

    def __init__(
        self,
        checker: StepChecker,
        *,
        window: Window = 1,
        tolerance: float = 0.0,
        name: Optional[str] = None,
    ) -> None:
        if window not in (1, 2, "spec"):
            raise ValueError(
                f"unsupported window: {window!r}; expected 1, 2, or 'spec'"
            )
        if checker is None:
            raise ValueError("checker is required")
        self._checker = checker
        self._window: Window = window
        self._tolerance = float(tolerance)
        self.name = name or f"programmatic_w{_window_name(window)}"

    @property
    def window(self) -> Window:
        return self._window

    @property
    def tolerance(self) -> float:
        return self._tolerance

    def _kinds_for_window(self) -> list[CheckKind]:
        if self._window == 1:
            return ["local"]
        if self._window == 2:
            return ["local", "link"]
        return ["local", "link", "spec"]

    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        del final_answer, expected_answer  # 局部验证不依赖最终答案
        if not steps:
            return _empty_result(self.name)

        n = len(steps)
        kinds = self._kinds_for_window()
        prev: Optional[StepView] = None
        for step in steps:
            for kind in kinds:
                reason = self._checker(
                    step,
                    prev=prev,
                    task=task,
                    kind=kind,
                    tolerance=self._tolerance,
                )
                if reason:
                    return AttributionResult(
                        attributor=self.name,
                        step_index=int(step.index),
                        total_steps=n,
                        rationale=(
                            f"window={_window_name(self._window)} "
                            f"kind={kind}: {reason}"
                        ),
                    )
            prev = step

        return AttributionResult(
            attributor=self.name,
            step_index=None,
            total_steps=n,
            rationale=(
                f"abstain: no failure under window="
                f"{_window_name(self._window)} "
                f"(kinds={','.join(kinds)})"
            ),
        )


def make_programmatic_attributors(
    checker: StepChecker,
    *,
    tolerance: float = 0.0,
    windows: Optional[list[Window]] = None,
) -> list[ProgrammaticAttributor]:
    """便捷工厂：按窗口列表构造一组程序化归因器。"""
    if windows is None:
        windows = [1, 2, "spec"]
    return [
        ProgrammaticAttributor(checker, window=w, tolerance=tolerance)
        for w in windows
    ]


# 供类型检查与外部扩展使用
__all__ = [
    "CheckKind",
    "ProgrammaticAttributor",
    "StepChecker",
    "Window",
    "make_programmatic_attributors",
]
