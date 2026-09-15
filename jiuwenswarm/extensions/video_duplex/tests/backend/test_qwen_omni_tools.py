from __future__ import annotations

import json

import pytest

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    QWEN_OMNI_DELEGATE_TOOL_NAME,
    QWEN_OMNI_RESEARCH_TOOL_NAME,
    parse_qwen_omni_tool_call,
    qwen_omni_tools,
)


def test_qwen_tool_definition_exposes_only_jiuwen_delegate() -> None:
    tools = qwen_omni_tools()

    assert len(tools) == 1
    function = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert function["name"] == QWEN_OMNI_DELEGATE_TOOL_NAME
    assert function["parameters"]["required"] == ["task"]
    assert function["parameters"]["additionalProperties"] is False
    assert "all of its available capabilities" in function["description"]


def test_parse_qwen_delegate_accepts_complete_task() -> None:
    call = parse_qwen_omni_tool_call({
        "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
        "call_id": "call-123",
        "arguments": json.dumps({"task": "打开桌面的复习提纲并转换为 PDF"}),
    })

    assert call.call_id == "call-123"
    assert call.task == "打开桌面的复习提纲并转换为 PDF"
    assert call.query == call.task


def test_parse_qwen_tool_call_keeps_legacy_research_compatibility() -> None:
    call = parse_qwen_omni_tool_call({
        "name": QWEN_OMNI_RESEARCH_TOOL_NAME,
        "call_id": "call-legacy",
        "arguments": {"query": "香港今天的天气"},
    })

    assert call.task == "香港今天的天气"


@pytest.mark.parametrize("argument_name", ["query", "instruction", "request"])
def test_parse_qwen_delegate_accepts_model_argument_aliases(argument_name) -> None:
    call = parse_qwen_omni_tool_call({
        "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
        "call_id": f"call-{argument_name}",
        "arguments": {argument_name: "打开桌面文件"},
    })

    assert call.task == "打开桌面文件"


@pytest.mark.parametrize(
    "value",
    [
        {"name": "unknown", "call_id": "call-1", "arguments": '{"query":"x"}'},
        {"name": QWEN_OMNI_DELEGATE_TOOL_NAME, "call_id": "", "arguments": '{"task":"x"}'},
        {"name": QWEN_OMNI_DELEGATE_TOOL_NAME, "call_id": "call-1", "arguments": "{"},
        {
            "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
            "call_id": "call-1",
            "arguments": '{"task":"x","extra":true}',
        },
        {
            "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
            "call_id": "call-1",
            "arguments": {"task": 123},
        },
        {
            "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
            "call_id": "call-1",
            "arguments": {"unknown": "wrong schema"},
        },
    ],
)
def test_parse_qwen_tool_call_rejects_invalid_requests(value) -> None:
    with pytest.raises(ValueError):
        parse_qwen_omni_tool_call(value)
