# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process client for the shared Agent Runtime Public API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jiuwenswarm.runtime import AgentRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.runtime import (
        PreparedSessionProvision,
        SessionCreateInput,
        SessionCreateResult,
        SessionDeleteResult,
        SessionDescriptor,
        SessionForkInput,
        SessionForkResult,
        SessionProvisionCommitContext,
        SessionProvisionCommitTiming,
        SessionProvisionResult,
        SessionSwitchInput,
        SessionSwitchResult,
    )
    from jiuwenswarm.runtime.events import RuntimeEvent


class InProcessRuntimeClient:
    """Thin client with no server, protocol, socket, or transport concerns."""

    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        if runtime is not None:
            self._runtime = runtime
            return
        self._runtime = AgentRuntime()

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

    async def describe_session(
        self,
        *,
        session_id: str,
    ) -> SessionDescriptor | None:
        """Read persisted Session facts through the Runtime boundary."""
        return await self._runtime.describe_session(session_id=session_id)

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> PreparedSessionProvision[SessionCreateResult]:
        return await self._runtime.prepare_session_create(provision_input)

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> PreparedSessionProvision[SessionSwitchResult]:
        return await self._runtime.prepare_session_switch(provision_input)

    async def prepare_session_fork(
        self,
        provision_input: SessionForkInput,
    ) -> PreparedSessionProvision[SessionForkResult]:
        return await self._runtime.prepare_session_fork(provision_input)

    async def commit_session_provision(
        self,
        prepared: PreparedSessionProvision[SessionProvisionResult],
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext | None = None,
    ) -> SessionProvisionResult:
        return await self._runtime.commit_session_provision(
            prepared,
            timing=timing,
            context=context,
        )

    async def abort_session_provision(
        self,
        prepared: PreparedSessionProvision[SessionProvisionResult],
    ) -> None:
        await self._runtime.abort_session_provision(prepared)

    async def delete_session(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> SessionDeleteResult:
        return await self._runtime.delete_session(
            channel_id=channel_id,
            session_id=session_id,
        )

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        return self._runtime.stream(request)

    async def invoke(self, request: AgentRequest) -> list[RuntimeEvent]:
        """Invoke one non-streaming request through the shared Runtime."""
        return await self._runtime.invoke(request)

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
        # AgentRuntime.close() owns Session Coordinator and AgentManager
        # cleanup.  Team requests also keep a process-wide producer whose
        # stream deliberately survives a completed round, so stop only that
        # extra Runtime-owned stage before closing the Runtime itself.
        cleanup_errors: list[BaseException] = []
        try:
            await self._runtime.cancel_all_team_stream_tasks(
                reason="[process CLI close] ",
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            await self._runtime.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

__all__ = ["InProcessRuntimeClient"]
