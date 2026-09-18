# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The tool_result stream event carries the model-facing text next to the compat fields."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import ToolOutput
from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, AgentRail, ToolCallInputs
from openjiuwen.harness.tools.mobile_gui.rails.multimodal_skill_branch_rail import MultimodalSkillBranchRail

from jiuwenswarm.agents.harness.code.rails.code_plan_approval_rail import PlanApprovalRail
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail


class _StreamSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def write_stream(self, event: object) -> None:
        self.events.append(event)


def _ctx(session: _StreamSession, inputs: ToolCallInputs, exception: BaseException | None = None) -> SimpleNamespace:
    return SimpleNamespace(session=session, inputs=inputs, extra={}, exception=exception)


def test_after_tool_call_runs_after_every_rail_that_rewrites_the_result() -> None:
    rail = JiuSwarmStreamEventRail()
    producers = (
        PlanApprovalRail.priority,
        UserHookRail.priority,
        AgentRail.priority,
        MultimodalSkillBranchRail.priority,
    )

    assert rail.callback_priority(AgentCallbackEvent.AFTER_TOOL_CALL) < min(producers)
    assert rail.callback_priority(AgentCallbackEvent.BEFORE_TOOL_CALL) == JiuSwarmStreamEventRail.priority


@pytest.mark.asyncio
async def test_tool_result_event_adds_rendered_result_and_keeps_compat_result() -> None:
    session = _StreamSession()
    tool_result = ToolOutput(success=True, data={"matching_files": ["/a.py"]})
    inputs = ToolCallInputs(
        tool_call=SimpleNamespace(name="glob", id="tc-1"),
        tool_name="glob",
        tool_args={},
        tool_result=tool_result,
        tool_msg=ToolMessage(content="/a.py", tool_call_id="tc-1"),
    )

    await JiuSwarmStreamEventRail().after_tool_call(_ctx(session, inputs))

    payload = session.events[0].payload["tool_result"]
    assert payload["result"] == str(tool_result)
    assert payload["rendered_result"] == "/a.py"
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_failed_tool_call_renders_the_execution_error_message() -> None:
    session = _StreamSession()
    error = AbilityExecutionError(
        StatusCode.AGENT_TOOL_EXECUTION_ERROR,
        msg="boom",
        tool_message=ToolMessage(content="Tool execution error: boom", tool_call_id="tc-2"),
    )
    inputs = ToolCallInputs(tool_call=SimpleNamespace(name="bash", id="tc-2"), tool_name="bash", tool_args={})

    await JiuSwarmStreamEventRail().after_tool_call(_ctx(session, inputs, exception=error))

    payload = session.events[0].payload["tool_result"]
    assert payload["result"] == str(error)
    assert payload["rendered_result"] == "Tool execution error: boom"
