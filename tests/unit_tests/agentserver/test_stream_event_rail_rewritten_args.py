# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""StreamEventRail keeps tool arguments that an earlier rail rewrote.

An interrupt rail (priority 90) can approve a call with rewritten arguments
through ``approve(new_args=...)``, which sets ``ctx.inputs.tool_args``.
StreamEventRail (priority 80) runs after it and must clean those arguments,
not the model's original ``tool_call.arguments``.
"""

import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


def _ctx(model_args: dict, tool_args):
    tool_call = ToolCall(
        id="call-1", type="function", name="write", arguments=json.dumps(model_args)
    )
    return SimpleNamespace(
        session=_StreamSession(),
        inputs=ToolCallInputs(
            tool_call=tool_call, tool_name="write", tool_args=tool_args
        ),
        extra={},
        exception=None,
    )


@pytest.mark.asyncio
async def test_rewritten_arguments_reach_the_tool():
    """Arguments an approval rewrote are what the tool runs with."""
    model_args = {"path": "/etc/hosts", "content": "hi", "call_goal": "写入文件"}
    approved = {**model_args, "path": "/tmp/approved.txt"}
    ctx = _ctx(model_args, json.dumps(approved))

    await JiuSwarmStreamEventRail().before_tool_call(ctx)

    expected = {"path": "/tmp/approved.txt", "content": "hi"}
    assert json.loads(ctx.inputs.tool_args) == expected
    assert json.loads(ctx.inputs.tool_call.arguments) == expected


@pytest.mark.asyncio
async def test_call_goal_is_stripped_when_nothing_rewrote_the_arguments():
    """Without a rewrite the model's arguments are cleaned as before."""
    model_args = {"path": "a.txt", "content": "hi", "call_goal": "写入文件"}
    ctx = _ctx(model_args, json.dumps(model_args))

    await JiuSwarmStreamEventRail().before_tool_call(ctx)

    expected = {"path": "a.txt", "content": "hi"}
    assert json.loads(ctx.inputs.tool_args) == expected
    assert json.loads(ctx.inputs.tool_call.arguments) == expected
