# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK adaptation for supplemental input, without starting another chat turn."""

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.schema.interaction import InputDispatchMode

from jiuwenswarm.runtime.session_input import resolve_session_input_mode


class SessionInputDeliveryUnknown(RuntimeError):
    """The SDK returned across a closing boundary; do not retry automatically."""

    code = "SESSION_INPUT_DELIVERY_UNKNOWN"


def sdk_input_mode(params: Any) -> InputDispatchMode | None:
    mode = resolve_session_input_mode(params)
    return InputDispatchMode(mode.value) if mode is not None else None


class SessionInputGuard(AgentRail):
    """Close Host admission before the SDK's final-save/iteration-limit gap.

    This observes public callbacks only; it neither drains the steering queue
    nor drives the loop. A rejected input has not been submitted to the SDK.
    """

    def __init__(self, owner: Any):
        super().__init__()
        self.owner = owner
        self.accepting = False
        self._model_allows_steer = False
        self._active_tools = 0

    async def before_model_call(self, ctx):
        limit = getattr(ctx.agent.config, "max_iterations", 0)
        iteration = getattr(ctx.inputs, "react_iteration", 0)
        self._model_allows_steer = bool(limit and 0 < iteration < limit)
        self.accepting = self._model_allows_steer
        self._active_tools = 0

    async def after_model_call(self, ctx):
        if not getattr(getattr(ctx.inputs, "response", None), "tool_calls", None):
            self.accepting = False

    async def before_tool_call(self, ctx):
        self._active_tools += 1
        self.accepting = self._model_allows_steer

    async def after_tool_call(self, ctx):
        # Close when the last tool settles, but keep admission open for other
        # running tools. A subsequent serial tool also reopens its own window.
        self._active_tools = max(0, self._active_tools - 1)
        self.accepting = self._model_allows_steer and self._active_tools > 0

    async def on_model_exception(self, ctx):
        self.accepting = False

    async def on_tool_exception(self, ctx):
        self.accepting = False

    async def after_invoke(self, ctx):
        self.accepting = False
