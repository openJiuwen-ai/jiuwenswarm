# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mock exec tool that returns fixed error for SkillDev simulation."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

_EXEC_TOOL_CARD = ToolCard(
    id="skilldev_exec_tool",
    name="exec",
    description=(
        "os cli执行元工具。"
    ),
    input_params={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "组装好的完整命令行",
            },
        },
        "required": ["command"],
    },
)


async def _exec_tool_impl(**inputs: Any) -> dict[str, Any]:
    """Always return fixed error message - real execution requires physical device."""
    command = inputs.get("command", "")
    return {
        "success": False,
        "error": "当前工具不可用，需要使用真机测试",
        "command": command,
    }


def get_exec_tool() -> LocalFunction:
    return LocalFunction(card=_EXEC_TOOL_CARD, func=_exec_tool_impl)
