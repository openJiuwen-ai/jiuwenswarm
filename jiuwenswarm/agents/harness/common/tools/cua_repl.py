"""A single model-facing Python tool, with lossless MCP content conversion."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.foundation.tool.mcp.base import McpToolResult, extract_mcp_tool_result_content
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.tools.cua.user_activity import build_default_user_activity_probe

from .cua_runtime import desktop_lane


REPL_DESCRIPTION = (
    "Run Python with top-level await for multi-step desktop workflows. Preloaded: computer "
    "(Computer/App/Window/Element SDK), print, print_image. Variables persist across calls within this "
    "task. Await operations sequentially. Use print_image for screenshots and explicitly read "
    "to verify actions. Use references and coordinates from this tool; inspect a returned "
    "image before choosing coordinates in a later call. After errors, observe before retrying "
    "actions that may already have executed."
)


def convert_result(result) -> ToolOutput:
    extracted = extract_mcp_tool_result_content(result, include_image_content=True, tool_name="cua_repl")
    data = dict(extracted.data) if isinstance(extracted, McpToolResult) else {"content": extracted}
    data["mcp_meta"] = result.meta or {}
    if result.structuredContent is not None:
        data["structured_content"] = result.structuredContent
    # Preserve SDK error/effect text, without replaying or replacing its output.
    error = "\n".join(part.text for part in result.content if part.type == "text") if result.isError else None
    return ToolOutput(success=not bool(result.isError), data=data, error=error)


class CuaReplTool(Tool):
    def __init__(self, runtime, owner: str, *, pause_on_user_input=False):
        super().__init__(ToolCard(
            id=owner + ".cua_repl", name="cua_repl", description=REPL_DESCRIPTION,
            input_params={"type": "object", "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": 65536},
                "title": {"type": "string", "maxLength": 200},
            }, "required": ["code"], "additionalProperties": False},
            parallel_safe=False, stateless=False, idempotent=False,
        ))
        self.runtime = runtime
        self.probe = build_default_user_activity_probe() if pause_on_user_input else None
        self.last_input = (0, 0)
        self.last_failed_code, self.same_failures = None, 0

    async def invoke(self, inputs: Any, **kwargs):
        if not isinstance(inputs, dict) or set(inputs).difference({"code", "title"}):
            return self._error("cua_repl expects code and an optional title. No code was executed.")
        code, title = inputs.get("code"), inputs.get("title", "")
        if not isinstance(code, str) or not code.strip() or len(code) > 65536:
            return self._error("Invalid code or title. No code was executed.")
        if not isinstance(title, str) or len(title) > 200:
            return self._error("Invalid code or title. No code was executed.")
        if code == self.last_failed_code and self.same_failures >= 3:
            return self._error(
                "This exact code failed three times. Observe and correct the request; no code was replayed."
            )
        try:
            async with desktop_lane().acquire():
                await self.wait_for_idle()
                started = time.monotonic()
                try:
                    result = convert_result(await self.runtime.execute(code, title))
                finally:
                    self.last_input = (started - 1.5, time.monotonic() + 1.5)
        except asyncio.CancelledError:
            await asyncio.shield(self.runtime.close())
            raise
        except Exception as error:
            result = self._error(
                str(error) + " Earlier actions may have executed; observe before continuing. No automatic retry."
            )
        if not result.success:
            self.same_failures = self.same_failures + 1 if code == self.last_failed_code else 1
            self.last_failed_code = code
        else:
            self.same_failures, self.last_failed_code = 0, None
        return result

    async def wait_for_idle(self):
        if self.probe is None:
            return
        deadline, threshold = time.monotonic() + 60, 3
        while True:
            idle = self.probe.seconds_since_last_input()
            if idle is None or idle >= threshold or self.last_input[0] <= time.monotonic() - idle <= self.last_input[1]:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("User is still using the desktop. No REPL code was dispatched.")
            threshold = 5
            await asyncio.sleep(0.5)

    @staticmethod
    def _error(detail):
        return ToolOutput(success=False, data={"content": detail}, error=detail)

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
