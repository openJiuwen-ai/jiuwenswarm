# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prevent an implementation turn from ending without mutation evidence."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.rails.implement_completeness_rail import (
    CodeImplementationRuntime,
    get_code_implementation_runtime,
)


class CodeCompletionGateRail(DeepAgentRail):
    """Request another ReAct iteration while an implementation is incomplete.

    The cap prevents a broken model or unavailable test environment from
    looping forever. The preceding completeness rail injects the concrete
    reason into the next model call.
    """

    priority = 84

    def __init__(self, max_continuations: int = 3) -> None:
        super().__init__()
        self.max_continuations = max(1, int(max_continuations))
        self.runtime: CodeImplementationRuntime | None = None

    def init(self, agent: Any) -> None:
        self.runtime = get_code_implementation_runtime(agent)

    def uninit(self, agent: Any) -> None:
        self.runtime = None

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ModelCallInputs):
            return
        response = inputs.response
        if response is None or getattr(response, "tool_calls", None):
            return

        runtime = self.runtime
        if runtime is None:
            return
        status = runtime.state.completion_status()
        if status.complete or status.state == "not_implementation":
            return
        if runtime.gate_continuations >= self.max_continuations:
            ctx.request_force_finish(
                {
                    "output": (
                        "Code implementation stopped without completion evidence: "
                        f"{status.reason}"
                    ),
                    "result_type": "error",
                    "code": "CODE_COMPLETION_GATE_BLOCKED",
                }
            )
            return

        runtime.gate_continuations += 1
        ctx.request_model_continue()


__all__ = ["CodeCompletionGateRail"]
