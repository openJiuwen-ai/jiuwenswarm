# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Graceful finalization when the ReAct budget is exhausted.

``react_agent`` returns a bare ``Max iterations reached without completion``
error after the final tool result has already been written.  This rail runs on
``AFTER_REACT_ITERATION`` of the final allowed iteration and requests a
force-finish with an actionable message.  The ReAct for-else consumes that
request, so no change to the openjiuwen dependency is required.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger


class LastIterationFinalizeRail(DeepAgentRail):
    """Return an actionable partial-result message instead of a bare error."""

    priority: int = 96

    def __init__(self, *, language: str = "cn") -> None:
        super().__init__()
        self._language = "en" if str(language).lower().startswith("en") else "cn"
        self._max_iterations: int | None = None

    def set_language(self, language: str | None) -> None:
        self._language = "en" if str(language or "").lower().startswith("en") else "cn"

    async def before_invoke(self, ctx: Any) -> None:
        self._max_iterations = self._read_max_iterations(ctx)
        if self._max_iterations is None:
            logger.warning(
                "[LastIterationFinalizeRail] max_iterations unavailable; "
                "graceful finalize disabled"
            )

    async def after_react_iteration(self, ctx: Any) -> None:
        inputs = getattr(ctx, "inputs", None)
        try:
            iteration = int(getattr(inputs, "react_iteration", 0) or 0)
        except (TypeError, ValueError):
            return
        max_iterations = self._max_iterations
        if not isinstance(max_iterations, int) or max_iterations <= 0:
            return
        # react_iteration is 1-based.  AFTER_REACT_ITERATION carries
        # ModelCallInputs (not ToolCallInputs); a text-only final answer
        # breaks the loop before this hook fires, so response.tool_calls
        # is the completion signal available on this event.
        if iteration < max_iterations:
            return
        response = getattr(inputs, "response", None)
        if response is not None:
            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls is None and isinstance(response, dict):
                tool_calls = response.get("tool_calls")
            if not tool_calls:
                return

        if self._language == "en":
            output = (
                f"The maximum number of reasoning iterations "
                f"({iteration}/{max_iterations}) was reached and the task did "
                "not complete.\nNarrow the task scope or provide the missing data "
                "source, then retry."
            )
        else:
            output = (
                f"已达到最大思考轮数（{iteration}/{max_iterations}），任务未完成。\n"
                "建议：缩小任务范围，或补充数据源后重试。"
            )

        logger.warning(
            "[LastIterationFinalizeRail] final iteration session_id=%s "
            "iteration=%s/%s",
            self._session_id(ctx),
            iteration,
            max_iterations,
        )
        ctx.request_force_finish({"output": output, "result_type": "error"})

    @staticmethod
    def _read_max_iterations(ctx: Any) -> int | None:
        agent = getattr(ctx, "agent", None)
        for holder in (getattr(agent, "_react_agent", None), agent):
            for attr in ("_config", "config"):
                value = getattr(getattr(holder, attr, None), "max_iterations", None)
                if isinstance(value, int) and value > 0:
                    return value
        value = getattr(agent, "max_iterations", None)
        return value if isinstance(value, int) and value > 0 else None

    @staticmethod
    def _session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        try:
            return str(session.get_session_id() or "") if session is not None else ""
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["LastIterationFinalizeRail"]
