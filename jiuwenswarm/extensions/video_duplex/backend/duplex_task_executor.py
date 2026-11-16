"""Execute explicitly registered duplex tools through the existing Host task service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from jiuwenswarm.runtime.tool_calls.types import ToolCall, ToolExecutionContext, ToolResult
from .qwen_omni_tools import parse_qwen_omni_tool_call

if TYPE_CHECKING:
    from .qwen_omni_tools import QwenOmniToolCall
    from .task_adapter import VideoSearchManager


class JiuwenTaskExecution:
    def __init__(
        self,
        manager: VideoSearchManager,
        call: QwenOmniToolCall,
        context: ToolExecutionContext,
        *,
        frame_data_url: str = "",
        credential_resolver: Callable[[dict], Awaitable[str]] | None = None,
    ) -> None:
        self.manager, self.call, self.context = manager, call, context
        self.job_id = ""
        self.done = False
        self._operation = None
        self.frame_data_url = frame_data_url
        self.credential_resolver = credential_resolver

    async def wait(self) -> ToolResult:
        manager, call, context = self.manager, self.call, self.context
        from .mode_routing import resolve_execution_target

        target = await resolve_execution_target(manager.client, context.owner_id, context.scope)
        if call.name == "jiuwen_queue_manage":
            # Disallow answers inside the nested manager as well as at the public catalog.
            self._operation = asyncio.create_task(manager.manage_queue_call(
                context.owner_id, context.scope, call, call.task, allow_answers=False,
            ))
            receipt = await self._operation
            self.done = True
            if receipt.get("state") == "incomplete":
                return ToolResult(False)
            return ToolResult(True, json.dumps(receipt, ensure_ascii=False))
        credential_ref = await self.credential_resolver(target) if self.credential_resolver is not None else ""
        # Capture the durable identity before the next await, including cancellation.
        task = manager.submit_task(
            context.owner_id, context.scope, target, question=call.task, query=call.task,
            tool_call_id=call.call_id, tool_name=call.name, command_id=call.call_id,
            scheduling={key: call.arguments[key] for key in ("independent", "depends_on", "resources")
                        if key in call.arguments},
            title=call.arguments.get("title", ""),
            frame_data_url=self.frame_data_url, credential_ref=credential_ref,
        )
        self.job_id = task["id"]
        while True:
            task = manager.service.get(context.owner_id, context.scope, self.job_id)
            if task["status"] in {"completed", "failed", "cancelled"}:
                self.done = True
                return ToolResult(
                    task["status"] == "completed",
                    json.dumps(manager.public(task).get("result") or task.get("result") or {}, ensure_ascii=False),
                )
            await asyncio.sleep(0.1)

    async def cancel(self, reason: str) -> None:
        if self.done:
            return
        if self._operation is not None:
            # manage_queue_call shields its worker; retain and cancel that exact worker.
            await self.manager.cancel_queue_call(self.context.owner_id, self.context.scope, self.call.call_id)
            if not self._operation.done():
                self._operation.cancel()
            await asyncio.gather(self._operation, return_exceptions=True)
        if self.job_id:
            await self.manager.service.cancel(
                self.context.owner_id, self.context.scope, self.job_id,
                self.call.call_id + ":cancel",
            )
            async with asyncio.timeout(95):
                while True:
                    task = self.manager.service.get(self.context.owner_id, self.context.scope, self.job_id)
                    if task["status"] in {"completed", "failed", "cancelled"}:
                        return
                    if task["status"] == "unknown":
                        raise RuntimeError("Native task cancellation remains unsettled")
                    await asyncio.sleep(0.1)


class JiuwenTaskExecutor:
    def __init__(
        self,
        manager: VideoSearchManager,
        *,
        frame_lookup: Callable[[ToolCall], str] | None = None,
        credential_resolver: Callable[[dict], Awaitable[str]] | None = None,
    ) -> None:
        self.manager = manager
        self.frame_lookup = frame_lookup
        self.credential_resolver = credential_resolver

    async def start(self, call: ToolCall, context: ToolExecutionContext) -> JiuwenTaskExecution:
        if call.name not in {"jiuwen_delegate", "jiuwen_queue_manage"}:
            raise ValueError("Tool is not registered for this duplex session")
        # Only the wire adapter sees backend call_id. Every nested business operation
        # uses the namespaced identity, including the queue manager's child commands.
        parsed = parse_qwen_omni_tool_call({
            "name": call.name, "call_id": call.command_id, "arguments": call.arguments,
        })
        return JiuwenTaskExecution(
            self.manager, parsed, context,
            frame_data_url=self.frame_lookup(call) if self.frame_lookup is not None else "",
            credential_resolver=self.credential_resolver,
        )
