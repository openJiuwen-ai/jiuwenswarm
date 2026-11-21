"""工具卡继续使用旧接口可接受的对象/数组 Schema，不新增调用参数。"""

import pytest

from openjiuwen.harness.prompts.tools.todo import get_todo_create_input_params, get_todo_modify_input_params
from jiuwenswarm.agents.harness.code.prompt.code_todo_tool_prompts import (
    get_code_todo_create_input_params,
    get_code_todo_modify_input_params,
)
from jiuwenswarm.agents.harness.flash.tools.flash_todo import _unified_todo_input_params


@pytest.mark.parametrize("schema", [
    get_todo_create_input_params(), get_todo_modify_input_params(),
    get_code_todo_create_input_params(), get_code_todo_modify_input_params(),
    _unified_todo_input_params("cn"), _unified_todo_input_params("en"),
])
def test_todo_cards_keep_simple_schema_shapes_and_defined_required_fields(schema):
    def check(node):
        if isinstance(node, dict):
            assert not {"if", "then", "else", "oneOf", "allOf"} & node.keys()
            if node.get("type") == "object":
                assert set(node.get("required", [])) <= node.get("properties", {}).keys()
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    assert not {"plan_id", "revision", "read_authoritative", "tool_execution_status"} & schema["properties"].keys()
