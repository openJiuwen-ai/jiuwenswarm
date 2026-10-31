# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stateless observers for child invocation and tool callback boundaries."""

from time import monotonic

from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenswarm.common.subagent_logging import current_subagent_trace


class SubagentLoggingRail(AgentRail):
    """Observe before permission guards; no writes to shared callback extra."""

    # AgentRail orders higher priorities first, including forwarded child hooks.
    priority = 10000

    async def before_invoke(self, ctx):
        trace = current_subagent_trace()
        if trace is not None:
            trace.child_session_id = getattr(ctx.inputs, "conversation_id", None)
            trace.emit("invoke.start", pre_invoke_ms=round((monotonic() - trace.started) * 1000, 3))

    async def after_invoke(self, ctx):
        trace = current_subagent_trace()
        if trace is not None:
            result = getattr(ctx.inputs, "result", None)
            trace.raw_result_type = result.get("result_type") if isinstance(result, dict) else None
            trace.invoke_finished = monotonic()
            trace.emit("invoke.end", raw_result_type=trace.raw_result_type,
                       result_available=result is not None)

    async def before_tool_call(self, ctx):
        trace = current_subagent_trace()
        if trace is not None:
            inputs = ctx.inputs
            call_id = getattr(getattr(inputs, "tool_call", None), "id", "")
            trace.tool_calls += 1
            trace.tools[call_id] = (monotonic(), trace.tool_calls, inputs)
            trace.emit("tool.start", tool_call_id=call_id, tool_call=trace.tool_calls,
                       tool_name=getattr(inputs, "tool_name", ""))

    async def after_tool_call(self, ctx):
        self._finish_tool(ctx)

    async def on_tool_exception(self, ctx):
        self._finish_tool(ctx, exception=ctx.exception)

    @staticmethod
    def _finish_tool(ctx, exception=None):
        trace = current_subagent_trace()
        if trace is None:
            return
        inputs = ctx.inputs
        call_id = getattr(getattr(inputs, "tool_call", None), "id", "")
        started = trace.tools.pop(call_id, None)
        if started is None:  # Exception and after callbacks can both fire.
            return
        result = getattr(inputs, "tool_result", None)
        execution_started = getattr(inputs, "execution_started", False)
        success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
        status = "returned" if execution_started else "not_executed"
        if exception is not None:
            from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException

            status = "interrupted" if isinstance(exception, ToolInterruptException) else "error"
        elif success is False:
            status = "error"
        trace.emit("tool.end", tool_call_id=call_id, tool_call=started[1],
                   tool_name=getattr(inputs, "tool_name", ""), status=status,
                   execution_started=execution_started, success=success,
                   exception_type=type(exception).__name__ if exception is not None else None,
                   callback_duration_ms=round((monotonic() - started[0]) * 1000, 3))
