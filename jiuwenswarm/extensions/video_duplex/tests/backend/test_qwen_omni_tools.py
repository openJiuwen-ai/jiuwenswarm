from __future__ import annotations

import json

import pytest

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    QWEN_OMNI_RESEARCH_TOOL_NAME,
    parse_qwen_omni_tool_call,
    qwen_omni_tools,
)


def test_qwen_tool_definition_exposes_only_jiuwen_research() -> None:
    tools = qwen_omni_tools()

    assert len(tools) == 1
    function = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert function["name"] == QWEN_OMNI_RESEARCH_TOOL_NAME
    assert function["parameters"]["required"] == ["query"]
    assert function["parameters"]["additionalProperties"] is False


def test_parse_qwen_tool_call_accepts_json_arguments() -> None:
    call = parse_qwen_omni_tool_call({
        "name": QWEN_OMNI_RESEARCH_TOOL_NAME,
        "call_id": "call-123",
        "arguments": json.dumps({"query": "香港今天的天气"}),
    })

    assert call.call_id == "call-123"
    assert call.query == "香港今天的天气"


@pytest.mark.parametrize(
    "value",
    [
        {"name": "unknown", "call_id": "call-1", "arguments": '{"query":"x"}'},
        {"name": QWEN_OMNI_RESEARCH_TOOL_NAME, "call_id": "", "arguments": '{"query":"x"}'},
        {"name": QWEN_OMNI_RESEARCH_TOOL_NAME, "call_id": "call-1", "arguments": "{"},
        {
            "name": QWEN_OMNI_RESEARCH_TOOL_NAME,
            "call_id": "call-1",
            "arguments": '{"query":"x","extra":true}',
        },
        {
            "name": QWEN_OMNI_RESEARCH_TOOL_NAME,
            "call_id": "call-1",
            "arguments": {"query": 123},
        },
    ],
)
def test_parse_qwen_tool_call_rejects_invalid_requests(value) -> None:
    with pytest.raises(ValueError):
        parse_qwen_omni_tool_call(value)
