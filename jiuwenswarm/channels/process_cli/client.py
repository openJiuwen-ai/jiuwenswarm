# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process client for the shared Agent Runtime Public API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jiuwenswarm.runtime import AgentRuntime

_PROCESS_CLI_CHANNEL_ID = "process_cli"


class ProcessCliBoundaryError(ValueError):
    """Stable failure for a call that escapes the Process CLI Channel."""

    code = "PROCESS_CLI_CHANNEL_INVALID"
    retryable = False


def _require_process_cli_channel(channel_id: object) -> None:
    if channel_id != _PROCESS_CLI_CHANNEL_ID:
        raise ProcessCliBoundaryError(
            "process CLI Runtime calls require channel_id='process_cli'"
        )


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.runtime.agent_definition import (
        RuntimeAgentDefinition,
        RuntimeAgentExecution,
    )
    from jiuwenswarm.runtime.interaction import InteractionAnswerInput
    from jiuwenswarm.runtime.mcp_references import McpReferenceValidationResult
    from jiuwenswarm.runtime.mode_catalog import (
        ModeCatalogResult,
        RuntimeModeDescriptor,
    )
    from jiuwenswarm.runtime.model_catalog import (
        ModelCatalogResult,
        RuntimeModelDescriptor,
    )
    from jiuwenswarm.runtime.permission_catalog import (
        PermissionSnapshotInput,
        PermissionSnapshotResult,
    )
    from jiuwenswarm.runtime.session_catalog import (
        SessionGetInput,
        SessionListInput,
        SessionListResult,
        SessionSummary,
    )
    from jiuwenswarm.runtime.session_provisioner import (
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

    def validate_agent_definition(
        self,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
        *,
        mode: str,
    ) -> RuntimeAgentExecution:
        return self._runtime.validate_agent_definition(definition, mode=mode)

    def list_model_capabilities(
        self,
        *,
        current_selection: str = "",
    ) -> ModelCatalogResult:
        return self._runtime.list_model_capabilities(
            current_selection=current_selection
        )

    def resolve_model_capability(self, requested: str) -> RuntimeModelDescriptor:
        return self._runtime.resolve_model_capability(requested)

    def list_mode_capabilities(self) -> ModeCatalogResult:
        return self._runtime.list_mode_capabilities()

    def resolve_mode_capability(self, requested: object) -> RuntimeModeDescriptor:
        return self._runtime.resolve_mode_capability(requested)

    def get_session(self, request: SessionGetInput) -> SessionSummary | None:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return self._runtime.get_session(request)

    def list_sessions(self, request: SessionListInput) -> SessionListResult:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return self._runtime.list_sessions(request)

    def get_permission_snapshot(
        self,
        request: PermissionSnapshotInput,
    ) -> PermissionSnapshotResult:
        channel_id = getattr(request, "channel_id", None)
        session_id = getattr(request, "session_id", None)
        if session_id or channel_id:
            _require_process_cli_channel(channel_id)
        return self._runtime.get_permission_snapshot(request)

    def validate_mcp_references(
        self,
        references: Iterable[str],
    ) -> McpReferenceValidationResult:
        return self._runtime.validate_mcp_references(references)

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        _require_process_cli_channel(channel_id)
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
        descriptor = await self._runtime.describe_session(session_id=session_id)
        if descriptor is None:
            return None
        try:
            _require_process_cli_channel(getattr(descriptor, "channel_id", None))
        except ProcessCliBoundaryError:
            # A missing and a foreign Session are deliberately indistinguishable
            # at the Process CLI boundary.
            return None
        return descriptor

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> PreparedSessionProvision[SessionCreateResult]:
        _require_process_cli_channel(getattr(provision_input, "channel_id", None))
        return await self._runtime.prepare_session_create(provision_input)

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> PreparedSessionProvision[SessionSwitchResult]:
        _require_process_cli_channel(getattr(provision_input, "channel_id", None))
        return await self._runtime.prepare_session_switch(provision_input)

    async def prepare_session_fork(
        self,
        provision_input: SessionForkInput,
    ) -> PreparedSessionProvision[SessionForkResult]:
        _require_process_cli_channel(getattr(provision_input, "channel_id", None))
        return await self._runtime.prepare_session_fork(provision_input)

    async def commit_session_provision(
        self,
        prepared: PreparedSessionProvision[SessionProvisionResult],
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext | None = None,
    ) -> SessionProvisionResult:
        _require_process_cli_channel(
            getattr(getattr(prepared, "result", None), "channel_id", None)
        )
        return await self._runtime.commit_session_provision(
            prepared,
            timing=timing,
            context=context,
        )

    async def abort_session_provision(
        self,
        prepared: PreparedSessionProvision[SessionProvisionResult],
    ) -> None:
        _require_process_cli_channel(
            getattr(getattr(prepared, "result", None), "channel_id", None)
        )
        await self._runtime.abort_session_provision(prepared)

    async def delete_session(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> SessionDeleteResult:
        _require_process_cli_channel(channel_id)
        return await self._runtime.delete_session(
            channel_id=channel_id,
            session_id=session_id,
        )

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return self._runtime.stream(request)

    def stream_agent(
        self,
        request: AgentRequest,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
    ) -> AsyncIterator[RuntimeEvent]:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return self._runtime.stream_agent(request, definition)

    async def invoke(self, request: AgentRequest) -> list[RuntimeEvent]:
        """Invoke one non-streaming request through the shared Runtime."""
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return await self._runtime.invoke(request)

    async def invoke_agent(
        self,
        request: AgentRequest,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
    ) -> list[RuntimeEvent]:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return await self._runtime.invoke_agent(request, definition)

    async def answer_interaction(
        self,
        request: AgentRequest,
    ) -> list[RuntimeEvent]:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        return await self._runtime.answer_interaction(request)

    async def answer_interaction_input(
        self,
        answer: InteractionAnswerInput,
    ) -> list[RuntimeEvent]:
        _require_process_cli_channel(getattr(answer, "channel_id", None))
        return await self._runtime.answer_interaction_input(answer)

    def stream_interaction_answer(
        self,
        answer: InteractionAnswerInput,
    ) -> AsyncIterator[RuntimeEvent]:
        """Forward a typed answer while preserving incremental observations."""
        _require_process_cli_channel(getattr(answer, "channel_id", None))
        return self._runtime.stream_interaction_answer(answer)

    async def cancel(self, request: AgentRequest) -> None:
        _require_process_cli_channel(getattr(request, "channel_id", None))
        await self._runtime.cancel_request(request)

    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        _require_process_cli_channel(channel_id)
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


__all__ = ["InProcessRuntimeClient", "ProcessCliBoundaryError"]
