# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Built-in subagents' tool calls must reach the Web subagent panel (issue #8637).

The panel only receives what ``task_tool_events`` projects from the subagent
stream through the SDK ``ActivityProjector``, and pairs a call with its result
by ``tool_call_id``. These tests run the rail's chunks through that projector.
"""

from types import SimpleNamespace

import pytest
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import ToolOutput
from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.core.single_agent.rail.base import AgentRail, ToolCallInputs
from openjiuwen.harness.subagent_runtime.activity import ActivityProjector
from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig

from jiuwenswarm.agents.harness.common.rails.subagent_tool_activity_rail import (
    SubagentToolActivityRail,
)


class _StreamSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def write_stream(self, event: object) -> None:
        self.events.append(event)


class _BrokenSession:
    async def write_stream(self, event: object) -> None:
        raise RuntimeError("stream closed")


def _ctx(session, inputs: ToolCallInputs, exception: BaseException | None = None) -> SimpleNamespace:
    return SimpleNamespace(session=session, inputs=inputs, extra={}, exception=exception)


def _project(events: list[object]) -> list:
    projector = ActivityProjector(subagent_id="sub-1", config=SubagentRuntimeConfig())
    return [activity for event in events for activity in projector.project(event, task_id="sub-1")]


@pytest.mark.asyncio
async def test_tool_call_and_result_project_to_a_paired_panel_activity() -> None:
    session = _StreamSession()
    rail = SubagentToolActivityRail()
    inputs = ToolCallInputs(
        tool_call=SimpleNamespace(name="click", id="tc-1", arguments='{"x": 10, "y": 20}'),
        tool_name="click",
        tool_args={"x": 10, "y": 20},
    )

    await rail.before_tool_call(_ctx(session, inputs))
    inputs.tool_result = ToolOutput(success=True, data={"clicked": True})
    inputs.tool_msg = ToolMessage(content="clicked at (10, 20)", tool_call_id="tc-1")
    await rail.after_tool_call(_ctx(session, inputs))

    call, result = _project(session.events)
    assert (call.kind, call.tool_name, call.tool_call_id) == ("tool_call", "click", "tc-1")
    # The panel shows the arguments, so the JSON-string arguments must arrive parsed.
    assert call.summary == "click(x=10, y=20)"
    assert (result.kind, result.tool_call_id, result.ok) == ("tool_result", "tc-1", True)
    assert result.summary == "clicked at (10, 20)"


@pytest.mark.asyncio
async def test_failed_tool_is_shown_as_failed_with_the_model_facing_error() -> None:
    session = _StreamSession()
    error = AbilityExecutionError(
        StatusCode.AGENT_TOOL_EXECUTION_ERROR,
        msg="boom",
        tool_message=ToolMessage(content="Tool execution error: boom", tool_call_id="tc-2"),
    )
    inputs = ToolCallInputs(tool_call=SimpleNamespace(name="type_text", id="tc-2"), tool_name="type_text", tool_args={})

    await SubagentToolActivityRail().after_tool_call(_ctx(session, inputs, exception=error))

    (result,) = _project(session.events)
    assert result.ok is False
    assert result.summary == "Tool execution error: boom"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_result", [{"success": False}, {"status": "error"}, "[ERROR] page not found"])
async def test_plain_failure_result_is_not_shown_as_success(tool_result) -> None:
    # Tools that return a dict or text instead of a ToolOutput still report
    # failure; the panel must not mark them succeeded (same rule as the root rail).
    session = _StreamSession()
    inputs = ToolCallInputs(
        tool_call=SimpleNamespace(name="navigate", id="tc-4"),
        tool_name="navigate",
        tool_args={},
        tool_result=tool_result,
    )

    await SubagentToolActivityRail().after_tool_call(_ctx(session, inputs))

    (result,) = _project(session.events)
    assert result.ok is False


@pytest.mark.asyncio
async def test_stream_failure_never_breaks_the_subagent_tool_call() -> None:
    inputs = ToolCallInputs(tool_call=SimpleNamespace(name="click", id="tc-3"), tool_name="click", tool_args={})
    rail = SubagentToolActivityRail()

    await rail.before_tool_call(_ctx(_BrokenSession(), inputs))
    await rail.after_tool_call(_ctx(_BrokenSession(), inputs))


def test_runs_after_rails_that_reject_or_rewrite_the_tool_call() -> None:
    # Permission rails must be able to reject a call before the panel shows it,
    # and result-rewriting rails must finish before the result is shown.
    assert SubagentToolActivityRail.priority < AgentRail.priority


@pytest.mark.asyncio
async def test_result_inference_failure_never_breaks_the_subagent_tool_call(monkeypatch) -> None:
    # Working out success for the panel is display-only; if it throws on an
    # unexpected result shape, the tool call must still complete.
    from jiuwenswarm.agents.harness.common.rails import subagent_tool_activity_rail as rail_module

    def _boom(_result):
        raise ValueError("unexpected result shape")

    monkeypatch.setattr(rail_module, "_infer_tool_result_error", _boom)
    inputs = ToolCallInputs(
        tool_call=SimpleNamespace(name="navigate", id="tc-5"),
        tool_name="navigate",
        tool_args={},
        tool_result={"weird": object()},
    )

    await SubagentToolActivityRail().after_tool_call(_ctx(_StreamSession(), inputs))
