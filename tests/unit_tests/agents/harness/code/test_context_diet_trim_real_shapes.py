# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ContextDietTrimRail with the shapes agent-core really passes.

agent-core puts the model's raw JSON string in ``ToolCallInputs.tool_args``.
The rail used to keep ``tool_args`` only when it was a dict, so an explicit
``read_file`` window was trimmed anyway and the re-read hint lost the path.
"""

from __future__ import annotations

import json

import pytest
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.runner.runner import create_agent_session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.code.context_diet.config import ContextDietConfig
from jiuwenswarm.agents.harness.code.rails import ContextDietTrimRail


def _read_ctx(args: dict, lines: int) -> AgentCallbackContext:
    tool_call = ToolCall(
        id="c1", type="function", name="read_file", arguments=json.dumps(args)
    )
    content = "\n".join(f"{i:6d}\tline {i}" for i in range(1, lines + 1))
    return AgentCallbackContext(
        agent=None,
        session=create_agent_session(session_id="s-real"),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="read_file",
            tool_args=tool_call.arguments,  # agent-core passes the raw JSON string
            tool_result=ToolOutput(success=True, data={"content": content}),
            tool_msg=ToolMessage(content=content, tool_call_id="c1"),
        ),
        extra={},
    )


def _aggressive_rail(tmp_path) -> ContextDietTrimRail:
    trim = ContextDietConfig.from_mapping({"preset": "aggressive"}).trim
    assert trim.read_file_max_lines == 250
    rail = ContextDietTrimRail(trim, spill_root=tmp_path)
    rail.init(None)
    return rail


@pytest.mark.asyncio
async def test_explicit_read_window_in_json_string_args_is_not_trimmed(tmp_path):
    rail = _aggressive_rail(tmp_path)
    ctx = _read_ctx({"file_path": "big.py", "offset": 0, "limit": 400}, 400)

    await rail.after_tool_call(ctx)

    out = ctx.inputs.tool_msg.content.splitlines()
    assert len(out) == 400
    assert out[-1].endswith("line 400")


@pytest.mark.asyncio
async def test_unbounded_read_hint_names_the_file_from_json_string_args(tmp_path):
    rail = _aggressive_rail(tmp_path)
    ctx = _read_ctx({"file_path": "pkg/big.py"}, 400)

    await rail.after_tool_call(ctx)

    content = ctx.inputs.tool_msg.content
    assert "[context-diet]" in content
    assert "pkg/big.py" in content
