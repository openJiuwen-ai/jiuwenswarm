# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)

from jiuwenswarm.agents.harness.code.rails.code_completion_gate_rail import (
    CodeCompletionGateRail,
)
from jiuwenswarm.agents.harness.code.rails.implement_completeness_rail import (
    get_code_implementation_runtime,
)


def _ctx(agent) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(response=SimpleNamespace(tool_calls=[], content="done")),
    )


@pytest.mark.asyncio
async def test_gate_requests_another_model_round_for_incomplete_implementation():
    agent = SimpleNamespace()
    runtime = get_code_implementation_runtime(agent)
    runtime.state.start_turn("Fix the parser")
    rail = CodeCompletionGateRail(max_continuations=2)
    rail.init(agent)
    ctx = _ctx(agent)

    await rail.after_model_call(ctx)

    assert ctx.consume_model_continue_request() is True
    assert runtime.gate_continuations == 1


@pytest.mark.asyncio
async def test_gate_does_not_continue_non_implementation_answers():
    agent = SimpleNamespace()
    runtime = get_code_implementation_runtime(agent)
    runtime.state.start_turn("Explain how the parser works")
    rail = CodeCompletionGateRail()
    rail.init(agent)
    ctx = _ctx(agent)

    await rail.after_model_call(ctx)

    assert ctx.consume_model_continue_request() is False
    assert ctx.consume_force_finish() is None


@pytest.mark.asyncio
async def test_gate_returns_error_instead_of_accepting_incomplete_final_answer():
    agent = SimpleNamespace()
    runtime = get_code_implementation_runtime(agent)
    runtime.state.start_turn("Implement the parser")
    runtime.gate_continuations = 1
    rail = CodeCompletionGateRail(max_continuations=1)
    rail.init(agent)
    ctx = _ctx(agent)

    await rail.after_model_call(ctx)

    finish = ctx.consume_force_finish()
    assert finish is not None
    assert finish.result["result_type"] == "error"
    assert finish.result["code"] == "CODE_COMPLETION_GATE_BLOCKED"
