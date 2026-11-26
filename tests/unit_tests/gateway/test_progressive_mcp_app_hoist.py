# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CR-10997-03 回归：mcp_app 提升必须是移动语义（嵌套层不保留副本）。"""

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    _hoist_mcp_app_block,
)


def test_hoist_moves_block_and_leaves_no_nested_copy() -> None:
    payload = {
        "success": True,
        "tool_name": "t",
        "result": {"result": "ok", "mcp_app": {"server": "s", "toolResult": {"isError": False}}},
    }
    _hoist_mcp_app_block(payload["result"], payload)
    assert payload["mcp_app"] == {"server": "s", "toolResult": {"isError": False}}
    assert "mcp_app" not in payload["result"]


def test_hoist_ignores_non_dict_result() -> None:
    payload = {"success": True, "result": "plain"}
    _hoist_mcp_app_block(payload["result"], payload)
    assert "mcp_app" not in payload
