# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process client for the shared Agent Runtime Public API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jiuwenswarm.runtime import AgentRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.runtime.events import RuntimeEvent


class InProcessRuntimeClient:
    """Thin client with no server, protocol, socket, or transport concerns."""

    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self._runtime = runtime or AgentRuntime()

    @property
    def runtime(self) -> AgentRuntime:
        return self._runtime

    async def start(self) -> None:
        await self._runtime.start()

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        return await self._runtime.create_or_resume_session(
            channel_id=channel_id,
            session_id=session_id,
        )

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        return self._runtime.stream(request)

    async def answer_interaction(
        self,
        request: AgentRequest,
    ) -> list[RuntimeEvent]:
        return await self._runtime.answer_interaction(request)

    async def cancel(self, request: AgentRequest) -> None:
        await self._runtime.cancel_request(request)

    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        return await self._runtime.cleanup_session(
            channel_id=channel_id,
            session_id=session_id,
        )

    async def close(self) -> None:
        await self._runtime.close()


__all__ = ["InProcessRuntimeClient"]
