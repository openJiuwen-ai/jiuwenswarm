# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the last-iteration partial-result finalizer."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import ModelCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.last_iteration_finalize_rail import (
    LastIterationFinalizeRail,
)


def _ctx(iteration: int, max_iterations: int, *, tool_calls: bool = True):
    agent = SimpleNamespace(
        _react_agent=SimpleNamespace(
            _config=SimpleNamespace(max_iterations=max_iterations)
        )
    )
    response = SimpleNamespace(tool_calls=[SimpleNamespace()] if tool_calls else [])
    return SimpleNamespace(
        agent=agent,
        inputs=ModelCallInputs(react_iteration=iteration, response=response),
        request_force_finish=MagicMock(),
    )


@pytest.mark.asyncio
async def test_no_finish_before_last_iteration() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=2, max_iterations=3)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_last_iteration_with_model_call_inputs_returns_actionable_error() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "error"
    assert "最大思考轮数" in payload["output"]
    assert "建议" in payload["output"]


@pytest.mark.asyncio
async def test_text_only_response_does_not_force_finish() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3, tool_calls=False)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_set_language_switches_to_english() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3)
    await rail.before_invoke(ctx)
    rail.set_language("en")

    await rail.after_react_iteration(ctx)

    payload = ctx.request_force_finish.call_args[0][0]
    assert "maximum number of reasoning iterations" in payload["output"]


@pytest.mark.asyncio
async def test_reads_max_iterations_from_agent_config_fallback() -> None:
    rail = LastIterationFinalizeRail()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(max_iterations=3)),
        inputs=ModelCallInputs(
            react_iteration=3,
            response=SimpleNamespace(tool_calls=[SimpleNamespace()]),
        ),
        request_force_finish=MagicMock(),
    )
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_called_once()


class _ToolCallModel:
    def __init__(self) -> None:
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid"
        )
        self.model_config = ModelRequestConfig(model_name="fake")

    async def stream(self, messages, **kwargs):
        yield AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", name="echo", arguments="{}")],
        )

    async def invoke(self, messages, **kwargs):
        return AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", name="echo", arguments="{}")],
        )


class _EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(name="echo", description="echo"))

    async def invoke(self, inputs, **kwargs):
        return "ok"

    async def stream(self, inputs, **kwargs):
        yield "ok"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_react_loop_uses_model_call_inputs_on_last_iteration(tmp_path) -> None:
    await Runner.start()
    agent = create_deep_agent(
        model=_ToolCallModel(),
        tools=[_EchoTool()],
        system_prompt="last-iteration contract test",
        max_iterations=1,
        workspace=str(tmp_path),
        agent_ras=False,
        enable_security_rail=False,
        enable_llm_retry_rail=False,
        rails=[LastIterationFinalizeRail()],
    )
    await agent.start(session=Session(session_id="last-iter-" + uuid.uuid4().hex))
    try:
        result = await agent.invoke({"query": "run the echo tool"})
    finally:
        await agent.stop()

    assert result["result_type"] == "error"
    assert "最大思考轮数" in result["output"]
    assert "Max iterations reached without completion" not in result["output"]
