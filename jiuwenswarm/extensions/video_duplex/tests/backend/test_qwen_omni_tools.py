from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    QWEN_OMNI_DELEGATE_TOOL_NAME,
    QWEN_OMNI_RESEARCH_TOOL_NAME,
    parse_qwen_omni_tool_call,
    qwen_omni_tools,
)


def test_qwen_tool_definition_exposes_delegate_and_task_controls() -> None:
    tools = qwen_omni_tools()

    assert {t["function"]["name"] for t in tools} == {
        "jiuwen_delegate",
        "jiuwen_task_query",
        "jiuwen_task_cancel",
        "jiuwen_task_modify",
        "jiuwen_task_reorder",
        "jiuwen_task_answer",
    }
    function = next(
        t["function"]
        for t in tools
        if t["function"]["name"] == QWEN_OMNI_DELEGATE_TOOL_NAME
    )
    assert tools[0]["type"] == "function"
    assert function["name"] == QWEN_OMNI_DELEGATE_TOOL_NAME
    assert function["parameters"]["required"] == ["task"]
    assert function["parameters"]["additionalProperties"] is False
    required = {
        "jiuwen_delegate": {"task"}, "jiuwen_task_query": set(),
        "jiuwen_task_cancel": {"job_id"},
        "jiuwen_task_modify": {"job_id", "revision", "instruction"},
        "jiuwen_task_reorder": {"job_id", "queue_version", "action"},
        "jiuwen_task_answer": {"job_id", "interaction_id", "answers"},
    }
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    for name, schema in schemas.items():
        assert set(schema["required"]) == required.get(name)
        assert schema["additionalProperties"] is False
    assert schemas["jiuwen_task_answer"]["properties"]["answers"]["items"]["type"] == "string"
    assert schemas["jiuwen_task_reorder"]["properties"]["action"]["enum"] == ["next", "before"]
    for name, field in [("jiuwen_task_modify", "revision"), ("jiuwen_task_reorder", "queue_version")]:
        assert schemas[name]["properties"][field]["type"] == "integer"


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


CONTRACT_CASES = json.loads((Path(__file__).parents[1] / "task_tool_contract.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda case: case["id"])
def test_shared_task_tool_contract(case):
    value = {"name": case["name"], "call_id": case["id"], "arguments": json.dumps(case["arguments"])}
    if case["server_accepts"]:
        assert parse_qwen_omni_tool_call(value).arguments == case["arguments"]
    else:
        with pytest.raises(ValueError):
            parse_qwen_omni_tool_call(value)
