# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CircuitBreakerRail and paused tool calls.

openjiuwen fires ``after_tool_call`` for a call that a ``before_tool_call``
rail interrupted (a permission approval, ask_user), with
no result and ``ctx.exception`` set to the ``ToolInterruptException``. Such a
call has no outcome yet; its result arrives when the turn resumes.
"""

from __future__ import annotations

import json

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    CircuitBreakerRail,
)


async def _start(rail: CircuitBreakerRail) -> AgentCallbackContext:
    ctx = AgentCallbackContext(
        agent=None, inputs=InvokeInputs(query="q", conversation_id="session-1")
    )
    await rail.before_invoke(ctx)
    return ctx


def _call_ctx(
    start: AgentCallbackContext, index: int, *, result=None, paused: bool = False
) -> AgentCallbackContext:
    call = ToolCall(
        id=f"call_{index}",
        type="function",
        name="read",
        arguments=json.dumps({"filepath": f"f{index}.txt"}),
    )
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(tool_call=call, tool_result=result),
        extra=dict(start.extra),
    )
    if paused:
        ctx.exception = ToolInterruptException(
            request=InterruptRequest(message="confirm"), tool_call=call
        )
    return ctx


@pytest.mark.asyncio
async def test_paused_calls_do_not_trip_the_breaker() -> None:
    """Fifteen paused calls of one tool in a turn leave the turn running."""
    rail = CircuitBreakerRail()
    start = await _start(rail)
    for index in range(15):
        ctx = _call_ctx(start, index, paused=True)
        await rail.after_tool_call(ctx)
        assert ctx.consume_force_finish() is None, f"pause {index + 1} stopped the turn"


@pytest.mark.asyncio
async def test_failed_calls_still_trip_the_breaker() -> None:
    """Ten failed calls of one tool still stop the turn at the tenth."""
    rail = CircuitBreakerRail()
    start = await _start(rail)
    finishes = []
    for index in range(10):
        ctx = _call_ctx(
            start, index, result={"success": False, "error": "unknown tool"}
        )
        await rail.after_tool_call(ctx)
        finishes.append(ctx.consume_force_finish())
    assert finishes[-1] is not None and all(finish is None for finish in finishes[:-1])
