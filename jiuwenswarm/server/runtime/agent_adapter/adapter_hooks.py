# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Injected extension boundary for execution adapters.

The default implementation resolves extension infrastructure only when a cloud
memory request actually uses it. Hosts may supply another implementation without
making execution or stream projection depend on the extension registry.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol


class AdapterHookServices(Protocol):
    async def sync_conversation_context(self, harness: Any, session_id: str) -> None:
        """Synchronize a managed conversation at its authorized input boundary."""

    async def before_memory(self, request: Any) -> str:
        """Return the memory block authorized for this request."""

    async def after_memory(self, request: Any, assistant_message: str) -> None:
        """Commit the completed assistant turn to the memory provider."""

    async def install_task_rail(self, harness: Any, rail: Any, *, reload: bool = False) -> Any:
        """Return the host-managed task callback rail for this harness."""

    def bind_task_execution(self, request: Any, adapter: Any, inputs: dict) -> AbstractAsyncContextManager:
        """Bind managed-task execution for exactly one invocation."""

    async def bind_task_output(self, rail: Any, request: Any, stream: Any) -> None:
        """Bind the exact output lease used by a managed invocation."""

    async def close_task_output(self, rail: Any, request: Any) -> None:
        """Close a managed output lease after its round is cancelled."""


class ExtensionAdapterHookServices:
    async def sync_conversation_context(self, harness: Any, session_id: str) -> None:
        from jiuwenswarm.extensions.video_duplex.backend.conversation_context import sync_conversation_context

        await sync_conversation_context(harness, session_id)

    async def install_task_rail(self, harness: Any, rail: Any, *, reload: bool = False) -> Any:
        from jiuwenswarm.server.runtime.video_task.rail import install_task_rail

        return await install_task_rail(harness, rail, reload=reload)

    @asynccontextmanager
    async def bind_task_execution(self, request: Any, adapter: Any, inputs: dict):
        # Ordinary channels never need the concrete managed-video task host.
        # The implementation still owns all managed-session eligibility rules.
        if request.channel_id != "video_tool":
            yield
            return
        from jiuwenswarm.server.runtime.video_task.execution import bind_task_execution

        async with bind_task_execution(request, adapter, inputs):
            yield

    async def bind_task_output(self, rail: Any, request: Any, stream: Any) -> None:
        if rail is None:
            return
        from jiuwenswarm.server.runtime.video_task.execution import bind_task_output

        return await bind_task_output(rail, request, stream)

    async def close_task_output(self, rail: Any, request: Any) -> None:
        if rail is None:
            return
        from jiuwenswarm.server.runtime.video_task.execution import close_task_output

        return await close_task_output(rail, request)

    @staticmethod
    def _context(request: Any, *, assistant_message: str | None = None):
        from jiuwenswarm.common.utils import get_agent_home_dir
        from jiuwenswarm.common.hooks_context import MemoryHookContext

        fields = dict(
            session_id=request.session_id or "default",
            request_id=request.request_id or "",
            channel_id=request.channel_id,
            agent_name="main_agent",
            workspace_dir=str(get_agent_home_dir()),
            extra=request.params,
        )
        if assistant_message is not None:
            fields["assistant_message"] = assistant_message
        return MemoryHookContext(**fields)

    async def before_memory(self, request: Any) -> str:
        from jiuwenswarm.common.hook_event import AgentServerHookEvents
        from gateway.extensions.registry import ExtensionRegistry

        context = self._context(request)
        await ExtensionRegistry.get_instance().trigger(
            AgentServerHookEvents.MEMORY_BEFORE_CHAT, context
        )
        return "\n\n".join(block for block in context.memory_blocks if block)

    async def after_memory(self, request: Any, assistant_message: str) -> None:
        from jiuwenswarm.common.hook_event import AgentServerHookEvents
        from gateway.extensions.registry import ExtensionRegistry

        await ExtensionRegistry.get_instance().trigger(
            AgentServerHookEvents.MEMORY_AFTER_CHAT,
            self._context(request, assistant_message=assistant_message),
        )
