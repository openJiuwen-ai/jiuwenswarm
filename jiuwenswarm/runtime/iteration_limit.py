# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Report a Process CLI Agent's iteration limit without SDK message matching."""

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
    ModelCallInputs,
)


class ProcessCliIterationLimitRail(AgentRail):
    """Stop at a configured boundary and preserve the reason in the result.

    The inner loop is configured with one extra iteration. This lets a final
    interrupted tool resume before the next model boundary reports the limit;
    the extra iteration never calls the model. Successful final answers and
    earlier force-finish requests keep their original outcomes.
    """

    def __init__(self, max_iterations: int) -> None:
        super().__init__()
        self.max_iterations = max_iterations

    def callback_priority(self, event: AgentCallbackEvent) -> int:
        # Observe other rails' finish requests before claiming a completed
        # iteration; reject the extra model call before ordinary model hooks.
        return -1000 if event == AgentCallbackEvent.AFTER_REACT_ITERATION else 1000

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if isinstance(inputs, ModelCallInputs):
            self._finish(ctx, inputs.react_iteration > self.max_iterations)

    async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if isinstance(inputs, ModelCallInputs):
            self._finish(ctx, inputs.react_iteration >= self.max_iterations)

    @staticmethod
    def _finish(ctx: AgentCallbackContext, exhausted: bool) -> None:
        if exhausted and not ctx.has_force_finish_request and ctx.exception is None:
            ctx.request_force_finish(
                {
                    "output": "Agent reached its iteration limit before completion.",
                    "result_type": "error",
                    "code": "MAX_ITERATIONS_REACHED",
                }
            )
