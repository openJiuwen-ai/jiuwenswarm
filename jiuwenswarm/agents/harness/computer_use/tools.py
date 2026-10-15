"""Stateful tools using the existing Jiuwen multimodal result contract."""

from __future__ import annotations

from typing import Any, AsyncIterator

from mcp import types
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.foundation.tool.mcp.base import (
    McpToolResult,
    extract_mcp_tool_result_content,
)
from openjiuwen.core.foundation.tool.schema import ToolOutput

from .runtime import ComputerUseRuntime


def convert_result(result: types.CallToolResult, name: str) -> ToolOutput:
    extracted = extract_mcp_tool_result_content(result, include_image_content=True, tool_name=name)
    if isinstance(extracted, McpToolResult):
        data = dict(extracted.data)
    else:
        data = {"content": extracted}
    data["mcp_meta"] = result.meta or {}
    if result.structuredContent is not None:
        data["structured_content"] = result.structuredContent
    return ToolOutput(
        success=not result.isError,
        data=data,
        error=extracted.error if isinstance(extracted, McpToolResult) and result.isError else None,
    )


class ComputerUseTool(Tool):

    def __init__(self, runtime: ComputerUseRuntime, *, reset: bool = False):
        name = "computer_use_reset" if reset else "computer_use"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        if not reset:
            parameters["properties"] = {
                "code": {"type": "string", "minLength": 1, "maxLength": 65536},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
            }
            parameters["required"] = ["code"]
        description = ("Clear this task's Python variables, imports, App/Element handles and capture streams. "
                       "Completed application actions remain; recreate handles and read afterwards." if reset else
                       "Execute Python with top-level await in this task's persistent Computer Use environment. "
                       "Preloaded: computer, print, printImage. Follow the Computer Use Skill. "
                       "Read explicitly to observe UI; actions return receipts without automatic reads. "
                       "Call serially. Never replay actions with uncertain effects.")
        super().__init__(
            ToolCard(
                id=f"{runtime.id}.{name}",
                name=name,
                description=description,
                input_params=parameters,
                parallel_safe=False,
                stateless=False,
                idempotent=reset,
            ))
        self.runtime = runtime
        self.startup_error: str | None = None

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> ToolOutput:
        if self.startup_error is not None:
            error = ("Computer Use startup failed; no UI command was dispatched. " + self.startup_error)
            return ToolOutput(success=False, data={"content": error}, error=error)
        arguments = {key: value for key, value in inputs.items() if value is not None}
        result = await self.runtime.call(self.card.name, arguments)
        return convert_result(result, self.card.name)

    async def stream(self, inputs: Any, **kwargs) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)
