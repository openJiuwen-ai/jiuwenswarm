"""Lifecycle coverage for Symphony graph-build call-level timeouts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.ability_manager import (
    AbilityExecutionError,
    AbilityManager,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.rails.tool_call_resilience_rail import ToolCallResilienceRail

from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyOrchestrationRail,
)


class _LifecycleCallbackManager:
    """Run the registered rails through AgentCallbackContext.fire()."""

    def __init__(self, rails: list[Any]) -> None:
        self._rails = sorted(rails, key=lambda rail: rail.priority, reverse=True)
        self.contexts: list[AgentCallbackContext] = []

    async def execute(self, event, ctx: AgentCallbackContext) -> AgentCallbackContext:
        self.contexts.append(ctx)
        for rail in self._rails:
            callback = rail.get_callbacks().get(event)
            if callback is not None:
                await callback(ctx)
        return ctx


class _ResourceManager:
    def __init__(self, tool: LocalFunction) -> None:
        self._tool = tool

    def get_tool(self, *, tool_id: str, **kwargs: Any) -> LocalFunction | None:
        del kwargs
        return self._tool if tool_id == self._tool.card.id else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["symphony_compose_graph", "symphony_refresh_graph"],
)
async def test_outer_symphony_timeout_terminates_without_retry(
    monkeypatch,
    tool_name,
):
    calls = 0
    force_finish_calls: list[AgentCallbackContext] = []
    original_request_force_finish = AgentCallbackContext.request_force_finish

    def track_force_finish(self, result):
        force_finish_calls.append(self)
        original_request_force_finish(self, result)

    monkeypatch.setattr(
        AgentCallbackContext,
        "request_force_finish",
        track_force_finish,
    )

    async def blocking_tool() -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    card = ToolCard(
        id=tool_name,
        name=tool_name,
        description=tool_name,
        input_params={"type": "object", "properties": {}},
        properties={"resilience": {"timeout_s": None}},
    )
    tool = LocalFunction(card=card, func=blocking_tool)
    resource_manager = _ResourceManager(tool)
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.resource_mgr",
        resource_manager,
        raising=False,
    )
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.ability_manager.MAX_TOOL_CALL_TIMEOUT_HARD_LIMIT",
        0.01,
    )

    ability_manager = AbilityManager()
    ability_manager.add(card)
    orchestration_rail = SymphonyOrchestrationRail(
        config_base={"symphony": {"enabled": True}},
    )
    resilience_rail = ToolCallResilienceRail()
    callback_manager = _LifecycleCallbackManager(
        [orchestration_rail, resilience_rail]
    )
    agent = SimpleNamespace(
        ability_manager=ability_manager,
        agent_callback_manager=callback_manager,
        system_prompt_builder=SystemPromptBuilder(language="cn"),
    )
    orchestration_rail.init(agent)
    invoke_ctx = AgentCallbackContext(agent=agent, extra={})
    tool_call = ToolCall(
        id=f"{tool_name}-call",
        type="function",
        name=tool_name,
        arguments="{}",
    )

    results = await ability_manager.execute(
        invoke_ctx,
        tool_call,
        session=None,
        parallel_tool_calls=False,
    )

    assert calls == 1
    payload, _tool_message = results[0]
    assert payload["success"] is False
    assert payload["reason"] == "graph_build_timeout"
    assert payload["direct_display"] is True
    assert payload["continue_after_display"] is False
    assert payload["followup_action"] == "manual_graph_build"
    force_finish = invoke_ctx.consume_force_finish()
    assert force_finish is not None
    assert force_finish.result == {
        "output": payload["content"],
        "result_type": "answer",
    }
    assert force_finish_calls.count(invoke_ctx) == 1
    assert len(force_finish_calls) == 2

    same_invoke_model_ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(
            tools=[
                SimpleNamespace(name="symphony_compose_graph"),
                SimpleNamespace(name="symphony_refresh_graph"),
                SimpleNamespace(name="other_tool"),
            ]
        ),
        extra=invoke_ctx.extra,
    )
    await orchestration_rail.before_model_call(same_invoke_model_ctx)
    assert [
        orchestration_rail._model_tool_name(tool)
        for tool in same_invoke_model_ctx.inputs.tools
    ] == ["other_tool"]
    assert "## Skill Orchestration Contract" not in agent.system_prompt_builder.build()

    new_invoke_model_ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(
            tools=[
                SimpleNamespace(name="symphony_compose_graph"),
                SimpleNamespace(name="symphony_refresh_graph"),
            ]
        ),
        extra={},
    )
    await orchestration_rail.before_model_call(new_invoke_model_ctx)
    assert [
        orchestration_rail._model_tool_name(tool)
        for tool in new_invoke_model_ctx.inputs.tools
    ] == ["symphony_compose_graph", "symphony_refresh_graph"]
    assert "## Skill Orchestration Contract" in agent.system_prompt_builder.build()

    exceptions = {
        id(ctx.exception): ctx.exception
        for ctx in callback_manager.contexts
        if ctx.exception is not None
    }
    assert len(exceptions) == 1
    exception = next(iter(exceptions.values()))
    assert isinstance(exception, AbilityExecutionError)
    assert isinstance(exception.__cause__, TimeoutError)
