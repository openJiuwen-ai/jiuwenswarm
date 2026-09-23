# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle owner for the shared JiuwenSwarm agent runtime.

This module deliberately has no transport concerns.  AgentServer and the
process-style CLI both own an ``AgentRuntime`` instance and use its public
operations; WebSocket framing remains in AgentServer.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from contextlib import aclosing
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_provisioner import (
    PreparedSessionProvision,
    RuntimeSessionProvisioner,
    SessionCreateInput,
    SessionCreateResult,
    SessionDeleteResult,
    SessionDescriptor,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionResult,
    SessionProvisionState,
    SessionSwitchInput,
    SessionSwitchResult,
)
from jiuwenswarm.runtime.session import (
    RuntimeSessionCoordinator,
    RuntimeSessionState,
    SessionCloseTimeoutError,
    SessionPersistencePolicy,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session_delete import TeamDeleteResult
from jiuwenswarm.runtime.session_lifecycle import (
    RuntimeParticipantRegistry,
    RuntimeResourceLease,
    SessionDescriptor as LifecycleSessionDescriptor,
    SessionExecutionEvent,
    SessionExecutionFinishedEvent,
    SessionInactiveEvent,
    SessionInputIntentDisposition,
    SessionInputIntentEvent,
    SessionKind,
    SessionLifecycleTarget,
)
from jiuwenswarm.runtime.session.model import (
    SessionExecutionSnapshot,
    SessionExecutionState,
)
from jiuwenswarm.runtime.session_input import resolve_session_input_mode, validate_session_input
from jiuwenswarm.server.runtime.agent_manager import AgentManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping

    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
    from jiuwenswarm.runtime.agent_definition import (
        RuntimeAgentDefinition,
        RuntimeAgentExecution,
    )
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.runtime.interaction import InteractionAnswerInput
    from jiuwenswarm.runtime.mcp_references import McpReferenceValidationResult
    from jiuwenswarm.runtime.mode_catalog import ModeCatalogResult, RuntimeModeDescriptor
    from jiuwenswarm.runtime.model_catalog import (
        ModelCatalogResult,
        RuntimeModelDescriptor,
    )
    from jiuwenswarm.runtime.permission_catalog import (
        PermissionSnapshotInput,
        PermissionSnapshotResult,
    )
    from jiuwenswarm.runtime.plan import PlanModeController
    from jiuwenswarm.runtime.session_catalog import (
        SessionGetInput,
        SessionListInput,
        SessionListResult,
        SessionSummary,
    )
    from jiuwenswarm.runtime.session_provisioner import SessionDeleteLifecycle

logger = logging.getLogger(__name__)

_SessionProvisionResultT = TypeVar(
    "_SessionProvisionResultT",
    bound=SessionProvisionResult,
)

_PROCESS_RUNTIME_DEPENDENCY_LOCK = asyncio.Lock()
_PROCESS_RUNTIME_DEPENDENCY_USERS = 0
_PROCESS_RUNTIME_EXTENSION_LOCK = asyncio.Lock()
_PROCESS_RUNTIME_EXTENSION_USERS = 0
_PROCESS_RUNTIME_EXTENSION_MANAGER: Any = None
_PROCESS_RUNTIME_EXTENSION_REGISTRY: Any = None


class RuntimeStateError(RuntimeError):
    """Raised when an operation violates the runtime lifecycle."""


async def _initialize_runtime_dependencies() -> None:
    """Initialize shared runtime dependencies without starting a server."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        ensure_persistent_checkpointer,
    )

    await ensure_persistent_checkpointer()


async def _acquire_process_runtime_dependencies() -> None:
    """Acquire the process-global checkpointer/Runner exactly once."""
    global _PROCESS_RUNTIME_DEPENDENCY_USERS

    async with _PROCESS_RUNTIME_DEPENDENCY_LOCK:
        if _PROCESS_RUNTIME_DEPENDENCY_USERS == 0:
            runner_start_attempted = False
            try:
                await _initialize_runtime_dependencies()
                from openjiuwen.core.runner import Runner

                runner_start_attempted = True
                runner_started = await Runner.start()
                if runner_started is False:
                    raise RuntimeError("Runner failed to start")
            except BaseException as start_error:
                from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                    close_persistent_checkpointer,
                )

                cleanup_errors: list[BaseException] = []
                if runner_start_attempted:
                    try:
                        runner_stopped = await Runner.stop()
                        if runner_stopped is False:
                            cleanup_errors.append(
                                RuntimeError("Runner failed to stop during rollback")
                            )
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                try:
                    await close_persistent_checkpointer()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                for cleanup_error in cleanup_errors:
                    logger.warning(
                        "Runtime dependency rollback failed while preserving %s: %s",
                        type(start_error).__name__,
                        cleanup_error,
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
                raise
        _PROCESS_RUNTIME_DEPENDENCY_USERS += 1


async def _release_process_runtime_dependencies() -> None:
    """Release shared dependencies after the final Runtime owner closes."""
    global _PROCESS_RUNTIME_DEPENDENCY_USERS

    async with _PROCESS_RUNTIME_DEPENDENCY_LOCK:
        if _PROCESS_RUNTIME_DEPENDENCY_USERS <= 0:
            return
        _PROCESS_RUNTIME_DEPENDENCY_USERS -= 1
        if _PROCESS_RUNTIME_DEPENDENCY_USERS > 0:
            return

        cleanup_errors: list[BaseException] = []
        from openjiuwen.core.runner import Runner

        try:
            runner_stopped = await Runner.stop()
            if runner_stopped is False:
                cleanup_errors.append(RuntimeError("Runner failed to stop"))
        except BaseException as exc:
            cleanup_errors.append(exc)

        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            close_persistent_checkpointer,
        )

        try:
            await close_persistent_checkpointer()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]


async def _acquire_process_runtime_extensions() -> bool:
    """Acquire extensions created by Runtime, preserving external ownership."""
    global _PROCESS_RUNTIME_EXTENSION_MANAGER
    global _PROCESS_RUNTIME_EXTENSION_REGISTRY
    global _PROCESS_RUNTIME_EXTENSION_USERS

    from openjiuwen.core.runner import Runner

    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    async with _PROCESS_RUNTIME_EXTENSION_LOCK:
        try:
            registry = ExtensionRegistry.get_instance()
        except RuntimeError:
            registry = ExtensionRegistry.create_instance(
                callback_framework=Runner.callback_framework,
                config={},
                logger=logger,
            )
            manager: ExtensionManager | None = None
            try:
                manager = ExtensionManager(registry=registry)
                await manager.load_all_extensions(include_transport_extensions=False)
            except BaseException as load_error:
                cleanup_error: BaseException | None = None
                if manager is not None:
                    try:
                        await manager.shutdown_all_extensions()
                    except BaseException as exc:
                        cleanup_error = exc
                try:
                    current = ExtensionRegistry.get_instance()
                except RuntimeError:
                    current = None
                if current is registry:
                    ExtensionRegistry.reset_instance()
                if cleanup_error is not None:
                    logger.warning(
                        "Runtime extension rollback failed while preserving %s: %s",
                        type(load_error).__name__,
                        cleanup_error,
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
                raise
            _PROCESS_RUNTIME_EXTENSION_MANAGER = manager
            _PROCESS_RUNTIME_EXTENSION_REGISTRY = registry
        else:
            if registry is not _PROCESS_RUNTIME_EXTENSION_REGISTRY:
                # AgentServer/Gateway may preload the registry. Runtime borrows it
                # and must not participate in or alter that owner's lifecycle.
                return False

        _PROCESS_RUNTIME_EXTENSION_USERS += 1
        return True


async def _release_process_runtime_extensions() -> None:
    """Release Runtime-owned extensions after the final Runtime closes."""
    global _PROCESS_RUNTIME_EXTENSION_MANAGER
    global _PROCESS_RUNTIME_EXTENSION_REGISTRY
    global _PROCESS_RUNTIME_EXTENSION_USERS

    from jiuwenswarm.extensions.registry import ExtensionRegistry

    async with _PROCESS_RUNTIME_EXTENSION_LOCK:
        if _PROCESS_RUNTIME_EXTENSION_USERS <= 0:
            return
        _PROCESS_RUNTIME_EXTENSION_USERS -= 1
        if _PROCESS_RUNTIME_EXTENSION_USERS > 0:
            return

        manager = _PROCESS_RUNTIME_EXTENSION_MANAGER
        registry = _PROCESS_RUNTIME_EXTENSION_REGISTRY
        _PROCESS_RUNTIME_EXTENSION_MANAGER = None
        _PROCESS_RUNTIME_EXTENSION_REGISTRY = None
        try:
            if manager is not None:
                await manager.shutdown_all_extensions()
        finally:
            try:
                current = ExtensionRegistry.get_instance()
            except RuntimeError:
                current = None
            if current is registry:
                ExtensionRegistry.reset_instance()


class AgentRuntime:
    """Own the existing ``AgentManager`` and its in-memory resources.

    The class is intentionally one-shot: after ``close`` it cannot be started
    again.  A process-style CLI creates one instance per command, while
    AgentServer owns one instance for its service lifetime.  A prepared Session
    provision is an outstanding Runtime operation: callers must commit or abort
    it before closing.  ``close`` rejects unfinished provisions before touching
    owned resources, so finalizers never run against a torn-down Runtime.
    """

    def __init__(
        self,
        *,
        agent_manager: AgentManager | None = None,
        initializer: Callable[[], Awaitable[None]] | None = None,
        plan_controller: PlanModeController | None = None,
        admission_controller: Any | None = None,
        session_delete_lifecycle: SessionDeleteLifecycle | None = None,
        session_coordinator: RuntimeSessionCoordinator | None = None,
        participant_registry: RuntimeParticipantRegistry | None = None,
        resource_lease: RuntimeResourceLease | None = None,
        team_execution_controller: object | None = None,
    ) -> None:
        self._agent_manager = agent_manager or AgentManager()
        self._initializer = initializer or _initialize_runtime_dependencies
        self._initialize_extensions = initializer is None
        self._manage_runner = initializer is None
        self._runner_started = False
        self._checkpointer_started = False
        self._shared_dependencies_acquired = False
        self._shared_extensions_acquired = False
        if plan_controller is None:
            from jiuwenswarm.runtime.plan import PlanModeController

            plan_controller = PlanModeController()
        self._plan_controller = plan_controller
        self._admission_controller = admission_controller
        self._session_message_service: Any | None = None
        self._participant_registry = (
            participant_registry or RuntimeParticipantRegistry()
        )
        self._session_provisioner = RuntimeSessionProvisioner(
            agent_manager=self._agent_manager,
            plan_controller=self._plan_controller,
            delete_lifecycle=session_delete_lifecycle,
            participant_registry=self._participant_registry,
            team_execution_controller=team_execution_controller,
        )
        self._resource_lease = resource_lease
        self._session_coordinator = session_coordinator or RuntimeSessionCoordinator()
        # Covers chat admission and preparation before the Team adapter creates
        # its own in-flight marker (including first-run Team construction).
        self._pending_chat_requests: dict[str, set[str]] = {}
        self._stateless_agents: dict[str, Any] = {}
        # A one-shot command may pause for one or more interactions before it
        # exits. Keep the declared root Agent pinned to that active Session so
        # answers and cancellation cannot fall back to the configured default
        # Agent. The declaration remains request-scoped and is never turned
        # into a second persisted Agent registry.
        self._agent_execution_owners: dict[
            tuple[str, str], RuntimeAgentExecution
        ] = {}
        self._agent_execution_owner_lock = asyncio.Lock()
        self._activity_executions: dict[
            tuple[str, str],
            tuple[SessionLifecycleTarget, tuple[Any, ...]],
        ] = {}
        if team_execution_controller is not None:
            set_reporter = getattr(
                team_execution_controller,
                "set_session_inactive_reporter",
                None,
            )
            if callable(set_reporter):
                set_reporter(self.record_session_inactive_by_id)
        self._lifecycle_lock = asyncio.Lock()
        self._session_provision_prepares = 0
        self._pending_session_provisions: set[PreparedSessionProvision[Any]] = set()
        self._started = False
        self._closed = False
        self.set_admission_controller(admission_controller)

    @property
    def agent_manager(self) -> AgentManager:
        """Return the single AgentManager owned by this runtime."""
        return self._agent_manager

    @property
    def plan_controller(self) -> PlanModeController:
        return self._plan_controller

    def set_admission_controller(self, controller: Any | None) -> None:
        """Attach optional host-owned scheduling admission to chat execution."""
        self._admission_controller = controller
        setter = getattr(controller, "set_session_message_blocker", None)
        if callable(setter):
            setter(self._session_message_goal_busy)

    def session_message_requires_queue(self, session_id: str) -> bool:
        """Prevent cross-session steering from interrupting plans or active goals."""
        return (
            session_id in self._plan_controller.active_sessions
            or self._session_message_goal_busy(session_id)
        )

    def _require_cross_session_input_admission(self, request: AgentRequest) -> None:
        from jiuwenswarm.common.mode_matrix import is_plan_mode
        from jiuwenswarm.runtime.session_input import SessionInputQueueRequiredError

        if isinstance(request.params.get(SESSION_MESSAGE_INTERNAL_KEY), dict) and (
            is_plan_mode(request.params.get("mode"))
            or self.session_message_requires_queue(request.session_id)
        ):
            raise SessionInputQueueRequiredError(
                "cross-session messages must queue while a plan or goal is active"
            )

    def _session_message_goal_busy(self, session_id: str) -> bool:
        """Check Goal ownership, including rounds without an output consumer."""
        snapshot = self._session_coordinator.snapshot_session(session_id)
        if snapshot is None:
            return False
        if any(
            execution.work_kind in {
                SessionWorkKind.GOAL_STREAM, SessionWorkKind.GOAL_ATTACH,
            }
            and not execution.state.terminal
            for execution in snapshot.executions
        ):
            return True
        checker = getattr(self._agent_manager, "has_active_goal", None)
        return bool(callable(checker) and checker(snapshot.channel_id, session_id))

    async def _mark_pending_interaction(self, event: RuntimeEvent) -> None:
        if event.event_type != "chat.ask_user_question":
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        await self._mark_pending_interaction_id(
            event.session_id or "default",
            str(payload.get("request_id") or ""),
        )

    async def _mark_pending_interaction_id(
        self, session_id: str, request_id: str
    ) -> None:
        request_id = str(request_id or "").strip()
        if not request_id:
            return
        marker = getattr(self._admission_controller, "mark_interaction_pending", None)
        if callable(marker):
            await marker(session_id, request_id)

    async def _clear_pending_interaction(
        self, session_id: str, request_id: str | None = None
    ) -> None:
        clearer = getattr(
            self._admission_controller,
            "clear_interaction_pending",
            None,
        )
        if callable(clearer):
            await clearer(session_id, request_id)

    @property
    def session_message_service(self) -> Any | None:
        """Return the optional AgentServer-owned cross-Session mailbox."""

        return self._session_message_service

    def set_session_message_service(self, service: Any | None) -> None:
        """Attach a transport-neutral Host capability used by Agent tools."""

        self._session_message_service = service

    def set_session_delete_lifecycle(
        self,
        lifecycle: SessionDeleteLifecycle | None,
    ) -> None:
        """Attach an optional non-transport Session deletion participant."""
        self._session_provisioner.set_delete_lifecycle(lifecycle)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        """Initialize runtime dependencies exactly once."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeStateError("runtime is already closed")
            if self._started:
                return
            try:
                if self._manage_runner:
                    await _acquire_process_runtime_dependencies()
                    self._shared_dependencies_acquired = True
                    self._checkpointer_started = True
                    self._runner_started = True
                else:
                    await self._initializer()
                if self._initialize_extensions:
                    await self._ensure_extensions()
                self._started = True
            except BaseException as start_error:
                try:
                    await self._rollback_start()
                except BaseException as cleanup_error:
                    logger.warning(
                        "Runtime start rollback failed while preserving %s: %s",
                        type(start_error).__name__,
                        cleanup_error,
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
                raise

    def validate_agent_definition(
        self,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
        *,
        mode: str,
    ) -> RuntimeAgentExecution:
        """Validate one custom root-Agent definition for this Runtime."""
        self._require_started()
        from jiuwenswarm.runtime.agent_definition import (
            RuntimeAgentDefinitionError,
            RuntimeAgentDefinitionErrorCode,
            prepare_agent_execution,
        )
        from jiuwenswarm.runtime.model_catalog import ModelCatalogError

        execution = prepare_agent_execution(definition, mode=mode)
        if execution.definition.model:
            try:
                self.resolve_model_capability(execution.definition.model)
            except ModelCatalogError as exc:
                raise RuntimeAgentDefinitionError(
                    "configured Agent model was not found",
                    code=RuntimeAgentDefinitionErrorCode.MODEL_NOT_FOUND,
                    field="model",
                ) from exc
        return execution

    def list_model_capabilities(
        self,
        *,
        current_selection: str = "",
    ) -> ModelCatalogResult:
        """Return executable configured models without connection secrets.

        The Adapter skips entries that it cannot construct.  Preserve each
        entry's original position while applying that same construction rule,
        so a catalog selection cannot silently resolve to the Adapter default.
        """
        self._require_started()
        from collections.abc import Mapping

        from jiuwenswarm.common.config import get_default_models
        from jiuwenswarm.runtime.model_catalog import build_model_catalog
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        try:
            configured_entries = tuple(get_default_models())
        except Exception:  # noqa: BLE001 - stable secret-free SDK boundary
            from jiuwenswarm.runtime.model_catalog import ModelCatalogError

            raise ModelCatalogError(
                "model catalog is unavailable",
                code="MODEL_CATALOG_UNAVAILABLE",
                retryable=True,
            ) from None
        catalog_entries: list[Mapping[str, Any] | None] = []
        for entry in configured_entries:
            if not isinstance(entry, Mapping):
                catalog_entries.append(None)
                continue
            client_config = entry.get("model_client_config")
            model_config = entry.get("model_config_obj")
            if not isinstance(client_config, Mapping) or (
                model_config is not None and not isinstance(model_config, Mapping)
            ):
                catalog_entries.append(None)
                continue
            try:
                build_model_from_entry(
                    dict(client_config),
                    dict(model_config or {}),
                )
            except Exception:  # noqa: BLE001 - match Adapter skip semantics
                catalog_entries.append(None)
            else:
                catalog_entries.append(entry)
        return build_model_catalog(
            catalog_entries,
            current_selection=current_selection,
        )

    def resolve_model_capability(self, requested: str) -> RuntimeModelDescriptor:
        """Resolve one SDK model selector using Adapter-compatible semantics."""
        self._require_started()
        from jiuwenswarm.runtime.model_catalog import resolve_model_selection

        return resolve_model_selection(
            self.list_model_capabilities(current_selection=requested),
            requested,
        )

    def list_mode_capabilities(self) -> ModeCatalogResult:
        """Return the stable single-Agent Runtime mode catalog."""
        self._require_started()
        from jiuwenswarm.runtime.mode_catalog import list_mode_capabilities

        return list_mode_capabilities()

    def resolve_mode_capability(self, requested: object) -> RuntimeModeDescriptor:
        """Resolve one supported single-Agent mode or legacy alias."""
        self._require_started()
        from jiuwenswarm.runtime.mode_catalog import resolve_mode_capability

        return resolve_mode_capability(requested)

    def get_session(self, request: SessionGetInput) -> SessionSummary | None:
        """Read one Channel-owned single-Agent Session."""
        self._require_started()
        from jiuwenswarm.runtime.session_catalog import get_session

        return get_session(request)

    def list_sessions(self, request: SessionListInput) -> SessionListResult:
        """List Channel-owned single-Agent Sessions after safe filtering."""
        self._require_started()
        from jiuwenswarm.runtime.session_catalog import list_sessions

        return list_sessions(request)

    def get_permission_snapshot(
        self,
        request: PermissionSnapshotInput,
    ) -> PermissionSnapshotResult:
        """Return one consistent, read-only permission snapshot."""
        self._require_started()
        from jiuwenswarm.runtime.permission_catalog import read_permission_snapshot

        return read_permission_snapshot(request)

    def validate_mcp_references(
        self,
        references: Iterable[str],
    ) -> McpReferenceValidationResult:
        """Validate MCP names locally without connecting or changing state."""
        self._require_started()
        from jiuwenswarm.runtime.mcp_references import validate_mcp_references

        return validate_mcp_references(references)

    async def _require_owned_single_agent_session(
        self,
        request: AgentRequest,
    ) -> None:
        """Fail closed before a new SDK call can adopt a foreign Session."""
        session_id = str(request.session_id or "").strip()
        if not session_id:
            return
        from jiuwenswarm.runtime.session_catalog import (
            SessionCatalogError,
            SessionGetInput,
        )

        active = self._session_coordinator.snapshot_session(session_id)
        if active is not None and active.state is not RuntimeSessionState.CLOSED:
            requested_channel = (
                str(request.channel_id or "default").strip().lower() or "default"
            )
            active_channel = str(active.channel_id or "default").strip().lower()
            if active_channel != requested_channel:
                raise SessionCatalogError("session not found", code="NOT_FOUND")

        owned = self.get_session(
            SessionGetInput(
                channel_id=request.channel_id or "default",
                session_id=session_id,
            )
        )
        if owned is not None:
            return
        if active is not None and active.state is not RuntimeSessionState.CLOSED:
            # A just-allocated Session has no metadata until its first turn.
            # Distinguish that legitimate in-lifecycle state from an already
            # persisted Team/Workflow Session, which the single-Agent SDK must
            # never adopt merely because the Coordinator knows its ID.
            descriptor = await self.describe_session(session_id=session_id)
            if descriptor is None:
                return
        # Missing, foreign-Channel and non-single-Agent Sessions deliberately
        # share one response so the SDK boundary does not reveal metadata.
        raise SessionCatalogError("session not found", code="NOT_FOUND")

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None = None,
    ) -> str:
        """Allocate a Runtime session id or retain an explicit persisted id."""
        self._require_started()
        requested = str(session_id or "").strip()
        if requested:
            from jiuwenswarm.server.runtime.session.session_history import (
                is_valid_session_id,
            )

            if not is_valid_session_id(requested):
                raise ValueError("invalid session_id")
            descriptor = await self.describe_session(session_id=requested)
            if descriptor is not None:
                requested_channel = str(channel_id or "default").strip().lower()
                persisted_channel = str(
                    descriptor.channel_id or "default"
                ).strip().lower()
                if persisted_channel != requested_channel:
                    from jiuwenswarm.runtime.session_catalog import (
                        SessionCatalogError,
                    )

                    # Do not reveal whether a syntactically valid ID belongs to
                    # another Channel, and never register it under the caller's
                    # in-memory ownership before this check.
                    raise SessionCatalogError("session not found", code="NOT_FOUND")
        resolved_session_id = await self._agent_manager.create_session(
            channel_id=channel_id,
            session_id=requested or None,
        )
        await self._register_session(
            session_id=resolved_session_id,
            channel_id=channel_id,
        )
        return resolved_session_id

    async def describe_session(
        self,
        *,
        session_id: str,
    ) -> SessionDescriptor | None:
        """Return persisted routing facts without exposing storage internals.

        This transport-neutral lookup lets local Runtime clients validate an
        explicit resume target without importing storage helpers or relying on
        AgentServer's ``session.list`` handler.
        """
        self._require_started()
        target = str(session_id or "").strip()
        if not target:
            return None

        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_history import (
            resolve_session_dir,
        )

        session_dir, _invalid_reason = resolve_session_dir(
            target,
            sessions_root=get_agent_sessions_dir(),
        )
        if session_dir is None or not session_dir.is_dir():
            return None

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(
            target,
            cache_bust=True,
            enable_writeback=False,
        )
        return SessionDescriptor(
            session_id=target,
            channel_id=str(metadata.get("channel_id") or "").strip(),
            mode=str(metadata.get("mode") or "").strip(),
            work_mode=str(metadata.get("work_mode") or "").strip().lower(),
            project_id=str(metadata.get("project_id") or "").strip(),
            project_dir=str(metadata.get("project_dir") or "").strip(),
            user_id=str(metadata.get("user_id") or "").strip(),
        )

    async def send_session_message(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        content: str,
        request_id: str | None = None,
    ) -> SessionExecutionSnapshot:
        """Queue a new turn in a persisted single-Agent Session.

        Delivery is asynchronous so two Sessions cannot deadlock by waiting on
        each other's model execution.  A target paused for control keeps its
        exact interaction owner; the new turn starts after that interaction is
        answered or cancelled.
        """
        await self.start()
        source_id = str(source_session_id or "").strip()
        target_id = str(target_session_id or "").strip()
        message = str(content or "").strip()
        if not source_id or not target_id:
            raise ValueError("source_session_id and target_session_id are required")
        if not message:
            raise ValueError("content is required")

        source = await self.describe_session(session_id=source_id)
        if source is None:
            raise ValueError(f"source session does not exist: {source_id}")
        target = await self.describe_session(session_id=target_id)
        if target is None:
            raise ValueError(f"target session does not exist: {target_id}")
        if not target.channel_id:
            raise ValueError(f"target session has no channel_id: {target_id}")
        if not self._is_single_agent_session_mode(
            target.mode,
            work_mode=target.work_mode,
        ):
            raise ValueError(f"target session is not Work/Code Normal: {target_id}")
        if source.user_id != target.user_id:
            raise PermissionError("source and target sessions have different owners")
        if not self._owns_session(target.session_id):
            await self._agent_manager.create_session(
                channel_id=target.channel_id,
                session_id=target.session_id,
            )
            await self._register_session(
                session_id=target.session_id,
                channel_id=target.channel_id,
            )

        from jiuwenswarm.common.schema.agent import AgentRequest

        message_request_id = str(request_id or "").strip() or uuid.uuid4().hex
        params = {
            "query": message,
            "mode": target.mode,
            "work_mode": target.work_mode,
        }
        if target.project_id:
            params["project_id"] = target.project_id
        if target.project_dir:
            params["project_dir"] = target.project_dir
        request = AgentRequest(
            request_id=message_request_id,
            channel_id=target.channel_id,
            session_id=target.session_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            metadata={"source_session_id": source.session_id},
            user_id=target.user_id,
        )
        return self._session_coordinator.submit_unary(
            target.session_id,
            message_request_id,
            SessionWorkKind.SESSION_MESSAGE,
            lambda: self._invoke_started(
                request,
                trigger_hook=True,
                on_control_event=None,
                agent_execution=None,
            ),
            suspension_key=self._waiting_control_id,
        )

    def get_session_execution(
        self,
        execution_id: str,
    ) -> SessionExecutionSnapshot | None:
        """Return the bounded status record for queued Session work."""
        return self._session_coordinator.get_execution(execution_id)

    async def prepare_session_fork(
        self,
        provision_input: SessionForkInput,
    ) -> PreparedSessionProvision[SessionForkResult]:
        """Prepare a transport-neutral Session fork on this Runtime."""
        async with self._lifecycle_lock:
            self._require_started()
            self._session_provision_prepares += 1

        prepared: PreparedSessionProvision[SessionForkResult] | None = None
        try:
            prepared = await self._session_provisioner.prepare_session_fork(
                provision_input
            )
            return prepared
        finally:
            self._session_provision_prepares -= 1
            if prepared is not None:
                self._pending_session_provisions.add(prepared)

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> PreparedSessionProvision[SessionCreateResult]:
        """Prepare a transport-neutral Session create on this Runtime."""
        async with self._lifecycle_lock:
            self._require_started()
            self._session_provision_prepares += 1

        prepared: PreparedSessionProvision[SessionCreateResult] | None = None
        try:
            prepared = await self._session_provisioner.prepare_session_create(
                provision_input
            )
            return prepared
        finally:
            self._session_provision_prepares -= 1
            if prepared is not None:
                self._pending_session_provisions.add(prepared)

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> PreparedSessionProvision[SessionSwitchResult]:
        """Prepare a transport-neutral Session switch on this Runtime."""
        async with self._lifecycle_lock:
            self._require_started()
            self._session_provision_prepares += 1

        prepared: PreparedSessionProvision[SessionSwitchResult] | None = None
        try:
            prepared = await self._session_provisioner.prepare_session_switch(
                provision_input
            )
            return prepared
        finally:
            self._session_provision_prepares -= 1
            if prepared is not None:
                self._pending_session_provisions.add(prepared)

    async def commit_session_provision(
        self,
        prepared: PreparedSessionProvision[_SessionProvisionResultT],
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext | None = None,
    ) -> _SessionProvisionResultT:
        """Commit a prepared Session operation before Runtime shutdown."""
        async with self._lifecycle_lock:
            self._require_started()
        try:
            result = await self._session_provisioner.commit_session_provision(
                prepared,
                timing=timing,
                context=context,
            )
            if isinstance(result, SessionCreateResult):
                mode = result.canonical_mode
                work_mode = result.work_mode
            elif isinstance(result, SessionSwitchResult):
                mode = result.mode
                work_mode = None
            else:
                mode = None
                work_mode = None
            if mode is not None and self._is_single_agent_session_mode(
                mode,
                work_mode=work_mode,
            ):
                await self._register_session(
                    session_id=result.session_id,
                    channel_id=result.channel_id,
                )
            return result
        finally:
            self._discard_finalized_session_provision(prepared)

    async def abort_session_provision(
        self,
        prepared: PreparedSessionProvision[_SessionProvisionResultT],
    ) -> None:
        """Abort a prepared Session operation before Runtime shutdown."""
        async with self._lifecycle_lock:
            self._require_started()
        try:
            await self._session_provisioner.abort_session_provision(prepared)
        finally:
            self._discard_finalized_session_provision(prepared)

    def _discard_finalized_session_provision(
        self,
        prepared: PreparedSessionProvision[Any],
    ) -> None:
        if prepared.state in {
            SessionProvisionState.COMMITTED,
            SessionProvisionState.ABORTED,
        }:
            self._pending_session_provisions.discard(prepared)

    async def _register_session(self, *, session_id: str, channel_id: str) -> None:
        """Adopt an existing product Session into this Runtime.

        Product create/switch and direct process callers converge here after
        durable lifecycle work, without reallocating the ID or touching
        metadata.
        """
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        from jiuwenswarm.server.runtime.session.lifecycle import claim_runtime

        claim_runtime(session_id)
        await self._session_coordinator.register_session(
            session_id,
            channel_id,
            SessionPersistencePolicy.PERSISTENT,
        )

    def _owns_session(self, session_id: str | None) -> bool:
        """Return whether the Coordinator owns the current Session generation."""
        snapshot = (
            self._session_coordinator.snapshot_session(session_id)
            if session_id
            else None
        )
        return bool(snapshot and snapshot.state is not RuntimeSessionState.CLOSED)

    @staticmethod
    def _is_single_agent_session_mode(
        mode: object,
        *,
        work_mode: object = None,
    ) -> bool:
        """Return whether a mode uses the single-Agent Session Runtime."""
        from jiuwenswarm.common.mode_matrix import (
            NEW_AGENT_CODE_NORMAL,
            NEW_AGENT_WORK_NORMAL,
            deprecate_mode,
        )
        from jiuwenswarm.runtime.request import resolve_agent_request_mode

        _mode, _sub_mode, canonical = resolve_agent_request_mode(
            mode,
            work_mode=work_mode,
        )
        return deprecate_mode(canonical) in {
            NEW_AGENT_WORK_NORMAL,
            NEW_AGENT_CODE_NORMAL,
        }

    async def _prepare_chat_turn(
        self,
        request: AgentRequest,
        channel_id: str,
        *,
        sync_metadata: bool = True,
        agent_execution: RuntimeAgentExecution | None = None,
    ) -> tuple[str, str | None, object]:
        """Resolve session semantics and return this Runtime's selected agent."""
        self._require_started()
        from jiuwenswarm.runtime.request import prepare_chat_turn

        prepare_kwargs: dict[str, Any] = {"sync_metadata": sync_metadata}
        if agent_execution is not None:
            prepare_kwargs.update(
                agent_definition=agent_execution.definition.to_dict(),
                agent_definition_fingerprint=agent_execution.fingerprint,
            )
        return await prepare_chat_turn(
            self._agent_manager,
            request,
            channel_id,
            **prepare_kwargs,
        )

    async def cancel_request(
        self,
        request: AgentRequest,
        *,
        allow_create: bool = False,
    ) -> AgentResponse:
        """Cancel the target request/session without crossing a transport."""
        # Cancellation must stay responsive while the first Runtime start is
        # still initializing the checkpointer/Runner.  Looking up an existing
        # Agent only needs the manager that is already constructed in __init__;
        # forcing start() here would wait on the lifecycle lock and defeat the
        # no-Agent fast-success path used by ESC during first-agent creation.
        if allow_create:
            # Agent creation can touch Runner/checkpointer-backed resources and
            # therefore retains the normal lifecycle barrier.  Only the
            # existing-Agent lookup path is safe during first initialization.
            await self.start()
        elif self._closed:
            raise RuntimeStateError("runtime is already closed")
        from jiuwenswarm.runtime.request import cancel_request

        response = await cancel_request(
            self._agent_manager,
            request,
            allow_create=allow_create,
        )
        params = request.params if isinstance(request.params, dict) else {}
        if (
            response.ok
            and str(params.get("intent") or "cancel") in {"cancel", "supplement"}
            and not (
                isinstance(response.payload, dict)
                and response.payload.get("success") is False
            )
        ):
            await self._clear_pending_interaction(request.session_id or "default")
        if request.session_id and self._owns_session(request.session_id):
            params = request.params if isinstance(request.params, dict) else {}
            target_request_id = str(params.get("target_request_id") or "").strip()
            await self._session_coordinator.cancel_execution(
                request.session_id,
                request_id=target_request_id or None,
            )
        return response

    async def cancel_all_inflight_work(
        self,
        reason: str = "[runtime cancel all] ",
        *,
        exclude_session_ids: Iterable[str] | None = None,
    ) -> None:
        """Cancel all existing Runtime work for a lost service host.

        A Gateway-to-AgentServer WebSocket represents the remote service host,
        not an individual end-user channel.  Preserve the established global
        disconnect semantics while keeping AgentManager ownership behind the
        Runtime public boundary.  This cleanup path intentionally does not
        start Runtime dependencies.
        """
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        excluded = None if exclude_session_ids is None else set(exclude_session_ids)
        await self._agent_manager.cancel_all_inflight_work(
            reason=reason,
            exclude_session_ids=excluded,
        )

    async def cancel_all_team_stream_tasks(
        self,
        reason: str = "[runtime cancel all team streams] ",
        *,
        exclude_session_ids: Iterable[str] | None = None,
    ) -> None:
        """Cancel process-wide Team streams without crossing a transport.

        This is a separate public cleanup stage so AgentServer can retain the
        established Agent cancellation, scheduler stop, then Team cancellation
        order.  It intentionally does not start Runtime dependencies.
        """
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        excluded = None if exclude_session_ids is None else set(exclude_session_ids)
        from jiuwenswarm.agents.harness.team import (
            cancel_all_team_stream_tasks_across_managers,
        )

        await cancel_all_team_stream_tasks_across_managers(
            reason=reason,
            exclude_session_ids=excluded,
        )

    def _bind_agent_execution_request(
        self,
        request: AgentRequest,
        execution: RuntimeAgentExecution,
    ) -> AgentRequest:
        """Bind a validated definition to a copy of one chat request."""
        from jiuwenswarm.runtime.agent_definition import (
            RuntimeAgentDefinitionError,
            RuntimeAgentDefinitionErrorCode,
        )

        if request.req_method is not ReqMethod.CHAT_SEND:
            raise RuntimeAgentDefinitionError(
                "custom Agent execution requires a chat.send request",
                code=RuntimeAgentDefinitionErrorCode.INVALID_REQUEST,
                field="request",
            )
        if not isinstance(request.params, dict):
            raise RuntimeAgentDefinitionError(
                "custom Agent execution requires object request params",
                code=RuntimeAgentDefinitionErrorCode.INVALID_REQUEST,
                field="request.params",
            )
        params = dict(request.params)
        params["mode"] = execution.mode.value
        params["work_mode"] = "code"
        if execution.definition.model:
            selected_model = self.resolve_model_capability(
                execution.definition.model
            )
            params["model_name"] = selected_model.selection_key
        return replace(request, params=params)

    @staticmethod
    def _agent_execution_owner_key(
        request: AgentRequest,
    ) -> tuple[str, str] | None:
        session_id = str(request.session_id or "").strip()
        if not session_id:
            return None
        channel_id = str(request.channel_id or "default").strip().lower() or "default"
        return channel_id, session_id

    async def _claim_agent_execution_owner(
        self,
        request: AgentRequest,
        execution: RuntimeAgentExecution,
    ) -> None:
        """Pin one declared root Agent to an active Runtime Session."""
        key = self._agent_execution_owner_key(request)
        if key is None:
            return
        from jiuwenswarm.runtime.agent_definition import (
            RuntimeAgentDefinitionError,
            RuntimeAgentDefinitionErrorCode,
        )

        async with self._agent_execution_owner_lock:
            current = self._agent_execution_owners.get(key)
            if current is not None and current.fingerprint != execution.fingerprint:
                raise RuntimeAgentDefinitionError(
                    "session is already bound to another Agent definition in this "
                    "Runtime lifecycle",
                    code=RuntimeAgentDefinitionErrorCode.SESSION_CONFLICT,
                    field="agent",
                )
            if current is not None:
                return
            self._agent_execution_owners[key] = execution

    def _agent_execution_owner(
        self,
        request: AgentRequest,
    ) -> RuntimeAgentExecution | None:
        key = self._agent_execution_owner_key(request)
        return self._agent_execution_owners.get(key) if key is not None else None

    async def _forget_agent_execution_owner(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> None:
        key = (
            str(channel_id or "default").strip().lower() or "default",
            str(session_id or "").strip(),
        )
        if not key[1]:
            return
        async with self._agent_execution_owner_lock:
            self._agent_execution_owners.pop(key, None)

    async def invoke_agent(
        self,
        request: AgentRequest,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Execute a declared root Agent through the existing Runtime chain."""
        await self.start()
        await self._require_owned_single_agent_session(request)
        params = request.params if isinstance(request.params, dict) else {}
        mode = self.resolve_mode_capability(params.get("mode")).mode
        execution = self.validate_agent_definition(definition, mode=mode)
        bound_request = self._bind_agent_execution_request(request, execution)
        await self._claim_agent_execution_owner(request, execution)
        return await self.invoke(
            bound_request,
            trigger_hook=trigger_hook,
            on_control_event=on_control_event,
            _agent_execution=execution,
        )

    async def stream_agent(
        self,
        request: AgentRequest,
        definition: RuntimeAgentDefinition | Mapping[str, Any],
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
        on_agent_ready: Callable[[Any], Any] | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Stream a declared root Agent through the existing Runtime chain."""
        await self.start()
        await self._require_owned_single_agent_session(request)
        params = request.params if isinstance(request.params, dict) else {}
        mode = self.resolve_mode_capability(params.get("mode")).mode
        execution = self.validate_agent_definition(definition, mode=mode)
        bound_request = self._bind_agent_execution_request(request, execution)
        await self._claim_agent_execution_owner(request, execution)
        async with aclosing(
            self.stream(
                bound_request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
                on_agent_ready=on_agent_ready,
                _agent_execution=execution,
            )
        ) as events:
            async for event in events:
                yield event

    async def invoke(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
        _agent_execution: RuntimeAgentExecution | None = None,
    ) -> list[RuntimeEvent]:
        """Execute one non-streaming request and return Runtime events."""
        await self.start()
        if self._is_session_input_request(request):
            async with aclosing(self.stream(
                request, trigger_hook=trigger_hook, on_control_event=on_control_event,
                _agent_execution=_agent_execution,
            )) as stream:
                return [event async for event in stream if event.payload is not None]
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        token = set_runtime_context(self, self._agent_manager)
        try:
            work_kind = self.session_work_kind(request)
            if work_kind is not None:
                await self._ensure_session_registered(request)
                if work_kind is SessionWorkKind.CONTROL_INPUT:
                    return await self._deliver_control(
                        request, on_control_event=on_control_event,
                    )
                return await self._session_coordinator.run_unary(
                    request.session_id or "default",
                    request.request_id,
                    work_kind,
                    lambda: self._invoke_started(
                        request,
                        trigger_hook=trigger_hook,
                        on_control_event=on_control_event,
                        agent_execution=_agent_execution,
                    ),
                    suspension_key=self._waiting_control_id,
                )
            return await self._invoke_started(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
                agent_execution=_agent_execution,
            )
        finally:
            reset_runtime_context(token)

    async def _invoke_started(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None,
        agent_execution: RuntimeAgentExecution | None,
    ) -> list[RuntimeEvent]:
        from jiuwenswarm.runtime.events import RuntimeEvent

        if trigger_hook:
            await self._trigger_before_chat_request_hook(request)
        channel_id = request.channel_id or "default"
        foreground = request.req_method in self._chat_turn_methods()
        activity_participants = (
            self._participant_registry.snapshot_activity() if foreground else ()
        )
        activity_execution_started = False
        activity_execution_succeeded = False
        admitted = (
            (foreground or self._starts_goal(request))
            and not self._request_targets_team(request)
        )
        interrupt_resume = self._is_interrupt_resume_request(request)
        interaction_answer = (
            interrupt_resume or request.req_method == ReqMethod.CHAT_ANSWER
        )
        if admitted and interrupt_resume:
            admitted = self._should_admit_interrupt_resume(request)
        foreground_started = False
        admission_started = False
        events: list[RuntimeEvent] = []
        agent: Any = None
        execution_error: Exception | None = None
        cancellation: asyncio.CancelledError | None = None
        readonly_goal_get = self._is_readonly_goal_get_request(request)
        stateless = self._is_stateless_method_request(request)
        try:
            if activity_participants:
                activity_execution_started = (
                    await self._record_session_execution_started(
                        request,
                        participants=activity_participants,
                    )
                )
            if foreground:
                await self._agent_manager.begin_foreground_chat()
                foreground_started = True
            if admitted and self._admission_controller is not None:
                await self._admission_controller.begin_user(
                    request.session_id or "default"
                )
                admission_started = True
            if interaction_answer:
                params = request.params if isinstance(request.params, dict) else {}
                await self._clear_pending_interaction(
                    request.session_id or "default",
                    str(params.get("request_id") or params.get("interaction_id") or ""),
                )
            if stateless:
                agent = await self._get_stateless_agent(channel_id)
            else:
                prepare_kwargs: dict[str, Any] = {
                    "sync_metadata": not readonly_goal_get
                }
                if agent_execution is not None:
                    prepare_kwargs["agent_execution"] = agent_execution
                mode, sub_mode, agent = await self._prepare_chat_turn(
                    request,
                    channel_id,
                    **prepare_kwargs,
                )
                if not readonly_goal_get:
                    plan_result = await self._plan_controller.ensure_state(
                        request,
                        mode,
                        sub_mode,
                        agent,
                    )
                    await self._emit_control_events(
                        request,
                        plan_result.events,
                        events=events,
                        handler=on_control_event,
                    )
            if self.uses_session_runtime(request):
                response = await agent.execute_message(request)
            else:
                response = await agent.process_message(request)
            event = RuntimeEvent.from_agent_message(
                response,
                request_id=request.request_id,
                channel_id=channel_id,
                session_id=request.session_id,
                default_agent_ref=request.agent_ref,
                default_complete=True,
            )
            await self._mark_pending_interaction(event)
            if admission_started and self._event_confirms_user_turn(event):
                await self._supersede_bypassed_session_messages(request)
            events.append(event)
            activity_execution_succeeded = True
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception as exc:  # noqa: BLE001
            execution_error = exc
            events.append(
                RuntimeEvent.error(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    session_id=request.session_id,
                    error=exc,
                    metadata=request.metadata,
                )
            )
        finally:
            plan_error: BaseException | None = None
            admission_error: BaseException | None = None
            end_error: BaseException | None = None
            try:
                if agent is not None and not stateless and not readonly_goal_get:
                    await self._emit_control_events(
                        request,
                        await self._plan_controller.check_post_process_exit(
                            request,
                            agent,
                        ),
                        events=events,
                        handler=on_control_event,
                    )
            except BaseException as exc:  # preserve execution/cancellation below
                plan_error = exc
            finally:
                if admission_started and self._admission_controller is not None:
                    try:
                        await self._admission_controller.end_user(
                            request.session_id or "default"
                        )
                    except BaseException as exc:
                        admission_error = exc
                if foreground_started:
                    try:
                        await self._agent_manager.end_foreground_chat()
                    except BaseException as exc:
                        end_error = exc
                if activity_execution_started:
                    self._record_session_execution_finished(
                        request,
                        succeeded=activity_execution_succeeded,
                    )

            primary_error: BaseException | None = cancellation or execution_error
            if primary_error is not None:
                self._log_suppressed_cleanup_error(
                    "plan post-processing",
                    plan_error,
                    primary_error,
                )
                self._log_suppressed_cleanup_error(
                    "chat admission cleanup",
                    admission_error,
                    primary_error,
                )
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    primary_error,
                )
            elif plan_error is not None:
                self._log_suppressed_cleanup_error(
                    "chat admission cleanup",
                    admission_error,
                    plan_error,
                )
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    plan_error,
                )
                raise plan_error
            elif admission_error is not None:
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    admission_error,
                )
                raise admission_error
            elif end_error is not None:
                raise end_error
        if cancellation is not None:
            raise cancellation
        return events

    async def answer_interaction(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Answer a paused Runtime interaction through the existing Agent."""
        if request.req_method != ReqMethod.CHAT_ANSWER:
            raise ValueError("interaction answer must use ReqMethod.CHAT_ANSWER")
        invoke_kwargs: dict[str, Any] = {
            "trigger_hook": trigger_hook,
            "on_control_event": on_control_event,
        }
        owner = self._agent_execution_owner(request)
        if owner:
            invoke_kwargs["_agent_execution"] = owner
        return await self.invoke(request, **invoke_kwargs)

    async def answer_interaction_input(
        self,
        answer: InteractionAnswerInput,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Answer either interaction protocol through one typed Runtime API."""
        from jiuwenswarm.runtime.interaction import InteractionAnswerInput

        if not isinstance(answer, InteractionAnswerInput):
            raise TypeError("answer must be an InteractionAnswerInput")
        await self.start()
        request = answer.to_agent_request()
        await self._require_owned_single_agent_session(request)
        if not answer.resumes_interrupted_turn:
            return await self.answer_interaction(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
            )
        stream_kwargs: dict[str, Any] = {
            "trigger_hook": trigger_hook,
            "on_control_event": on_control_event,
        }
        owner = self._agent_execution_owner(request)
        if owner:
            stream_kwargs["_agent_execution"] = owner
        return [event async for event in self.stream(request, **stream_kwargs)]

    async def stream_interaction_answer(
        self,
        answer: InteractionAnswerInput,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Stream a typed answer without buffering a resumed Agent execution.

        A live output owner keeps the continuing answer on its original stream;
        this operation may then emit only an acknowledgement. Its EOF is not a
        declaration that the Session or the caller's overall run has finished.
        The compatibility list APIs and other Channel execution paths are
        intentionally unchanged.
        """
        from jiuwenswarm.runtime.interaction import InteractionAnswerInput

        if not isinstance(answer, InteractionAnswerInput):
            raise TypeError("answer must be an InteractionAnswerInput")
        await self.start()
        request = answer.to_agent_request()
        await self._require_owned_single_agent_session(request)
        if not answer.resumes_interrupted_turn:
            events = await self.answer_interaction(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
            )
            for event in events:
                yield event
            return
        if self.session_work_kind(request) is SessionWorkKind.CONTROL_INPUT:
            await self._ensure_session_registered(request)
            stream = self._session_coordinator.deliver_control_stream(
                request.session_id or "default",
                self._control_request_id(request),
                lambda: self._stream_control_started(request),
                suspension_key=self._waiting_control_id,
            )
        else:
            stream = self.stream(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
                _agent_execution=self._agent_execution_owner(request),
            )
        async with aclosing(self._stream_with_runtime_context(stream)) as events:
            async for event in events:
                yield event

    async def _stream_with_runtime_context(
        self, stream: AsyncIterator[RuntimeEvent]
    ) -> AsyncIterator[RuntimeEvent]:
        """Bind only execution slices, never the caller's yield boundary."""
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        try:
            while True:
                token = set_runtime_context(self, self._agent_manager)
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    reset_runtime_context(token)
                yield event
        finally:
            token = set_runtime_context(self, self._agent_manager)
            try:
                close_stream = getattr(stream, "aclose", None)
                if callable(close_stream):
                    await close_stream()
            finally:
                reset_runtime_context(token)

    async def run_heartbeat(
        self,
        request: AgentRequest,
        operation: Callable[[], Awaitable[None]],
        *,
        timeout_seconds: float,
    ) -> None:
        """Own one admitted Heartbeat, including its execution deadline.

        The Heartbeat scheduler retains trigger/claim persistence and admission.
        The coordinator adopts the caller task, so its exact cancellation and
        Session close also await the caller's durable run-finalization cleanup.
        Do not enter the chat lane or foreground admission: an arriving user
        must be able to preempt this background execution.
        """
        if not request.session_id:
            raise ValueError("heartbeat session_id is required")
        # Adopt before initialization. The callback's stream starts Runtime
        # under the Heartbeat deadline, keeping cold startup cancellable too.
        await self._ensure_session_registered(request)
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        token = set_runtime_context(self, self._agent_manager)
        try:
            deadline = asyncio.timeout(timeout_seconds)
            timeout_error = (
                f"heartbeat execution timed out after {timeout_seconds:g} seconds"
            )
            try:
                async with deadline:
                    await self._session_coordinator.run_unary(
                        request.session_id,
                        request.request_id,
                        SessionWorkKind.HEARTBEAT,
                        operation,
                        wait_for_terminal=True,
                        timeout_scope=deadline,
                        timeout_error=timeout_error,
                    )
            except TimeoutError as exc:
                if not deadline.expired():
                    raise
                raise TimeoutError(timeout_error) from exc
        finally:
            reset_runtime_context(token)

    async def stream(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
        background: bool = False,
        on_agent_ready: Callable[[Any], Any] | None = None,
        _agent_execution: RuntimeAgentExecution | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Execute one request and yield the shared Runtime event stream."""
        await self.start()
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        is_session_input = self._is_session_input_request(request)
        if is_session_input:
            validate_session_input(request.params)
            if background:
                raise ValueError("session input must use foreground delivery")
            self._require_cross_session_input_admission(request)
            if not request.session_id or not self._is_single_agent_session_mode(
                request.params.get("mode"), work_mode=request.params.get("work_mode"),
            ):
                raise ValueError("session input requires a supported single-agent Session")
            snapshot = self._session_coordinator.snapshot_session(request.session_id)
            if snapshot is None:
                raise RuntimeStateError("session is not owned by this Runtime")
            if snapshot and snapshot.state in {RuntimeSessionState.CLOSED, RuntimeSessionState.QUIESCING}:
                raise RuntimeStateError("session is closing or closed")
        work_kind = self.session_work_kind(request, background=background)
        if work_kind is not None:
            await self._ensure_session_registered(request)
            if work_kind is SessionWorkKind.CONTROL_INPUT:
                events = await self._deliver_control(
                    request, on_control_event=on_control_event,
                )
                for event in events:
                    yield event
                return
            if work_kind is SessionWorkKind.SESSION_INPUT:
                self._require_cross_session_input_admission(request)

                async def idle_input():
                    from jiuwenswarm.runtime.events import RuntimeEvent

                    self._require_cross_session_input_admission(request)
                    # Web stream clients need the idle disposition before ordinary output.
                    # Unary clients must retain their single final response.
                    if request.is_stream:
                        yield RuntimeEvent(
                            request_id=request.request_id,
                            channel_id=request.channel_id or "default",
                            session_id=request.session_id,
                            payload={
                                "event_type": "runtime.accepted",
                                "request_id": request.request_id,
                                "session_id": request.session_id,
                                "input_delivery": "chat",
                            },
                        )
                    if not request.is_stream:
                        for event in await self._invoke_started(
                            request, trigger_hook=trigger_hook, on_control_event=on_control_event,
                            agent_execution=_agent_execution,
                        ):
                            yield event
                    else:
                        async with aclosing(self._stream_started(
                            request, trigger_hook=trigger_hook, on_control_event=on_control_event,
                            background=background, on_agent_ready=on_agent_ready,
                            agent_execution=_agent_execution,
                        )) as events:
                            async for event in events:
                                yield event

                stream = self._session_coordinator.stream_session_input(
                    request.session_id,
                    request.request_id,
                    lambda owner_channel: self._stream_session_input_started(request, owner_channel),
                    idle_input,
                    suspension_key=self._waiting_control_id,
                    expected_execution_id=request.params.get("expected_execution_id"),
                )
            else:
                stream = self._session_coordinator.run_stream(
                    request.session_id or "default",
                    request.request_id,
                    work_kind,
                    lambda: self._stream_started(
                        request,
                        trigger_hook=trigger_hook,
                        on_control_event=on_control_event,
                        background=background,
                        on_agent_ready=on_agent_ready,
                        agent_execution=_agent_execution,
                    ),
                    suspension_key=self._waiting_control_id,
                )
        else:
            stream = self._stream_started(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
                background=background,
                on_agent_ready=on_agent_ready,
                agent_execution=_agent_execution,
            )
        execution_id = None
        try:
            while True:
                # Never keep a ContextVar token across a yield boundary.  An
                # async generator may be finalized by another task/context;
                # resetting such a token there raises ValueError.  Each
                # execution slice still runs with the Runtime context, and
                # child tasks created by the agent inherit it normally.
                token = set_runtime_context(self, self._agent_manager)
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    reset_runtime_context(token)
                if execution_id is None and work_kind is not None:
                    snapshot = self._session_coordinator.snapshot_session(request.session_id or "default")
                    executions = snapshot.executions if snapshot else ()
                    execution = next(
                        (item for item in reversed(executions) if item.request_id == request.request_id), None,
                    )
                    if execution is not None:
                        execution_id = (
                            execution.parent_execution_id
                            if execution.work_kind is SessionWorkKind.SESSION_INPUT
                            else execution.execution_id
                        )
                if execution_id is not None and isinstance(event.payload, dict):
                    event.payload["execution_id"] = execution_id
                yield event
        finally:
            token = set_runtime_context(self, self._agent_manager)
            try:
                await stream.aclose()
            finally:
                reset_runtime_context(token)

    async def _stream_started(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None,
        background: bool,
        on_agent_ready: Callable[[Any], Any] | None,
        agent_execution: RuntimeAgentExecution | None,
    ) -> AsyncIterator[RuntimeEvent]:
        from jiuwenswarm.runtime.events import RuntimeEvent

        if trigger_hook:
            await self._trigger_before_chat_request_hook(request)
        channel_id = request.channel_id or "default"
        is_chat_turn = request.req_method in self._chat_turn_methods()
        foreground = is_chat_turn and not background
        activity_participants = (
            self._participant_registry.snapshot_activity() if foreground else ()
        )
        activity_execution_started = False
        activity_execution_succeeded = False
        admitted = (
            (is_chat_turn or self._starts_goal(request))
            and not background
            and not self._request_targets_team(request)
        )
        interrupt_resume = self._is_interrupt_resume_request(request)
        interaction_answer = (
            interrupt_resume or request.req_method == ReqMethod.CHAT_ANSWER
        )
        if admitted and interrupt_resume:
            admitted = self._should_admit_interrupt_resume(request)
        foreground_started = False
        admission_started = False
        agent: Any = None
        readonly_goal_get = self._is_readonly_goal_get_request(request)
        stateless = self._is_stateless_method_request(request)
        error: Exception | None = None
        cancellation: asyncio.CancelledError | None = None
        generator_exit: GeneratorExit | None = None
        supersede_attempted = False
        mailbox_turn = background and isinstance(
            (request.params or {}).get(SESSION_MESSAGE_INTERNAL_KEY), dict
        )
        pending_mailbox_interactions: set[str] = set()

        async def mark_interaction(event: RuntimeEvent) -> None:
            await self._mark_pending_interaction(event)
            if mailbox_turn:
                control_id = self._waiting_control_id(event)
                if control_id:
                    pending_mailbox_interactions.add(control_id)

        try:
            if activity_participants:
                activity_execution_started = (
                    await self._record_session_execution_started(
                        request,
                        participants=activity_participants,
                    )
                )
            if foreground:
                await self._agent_manager.begin_foreground_chat()
                foreground_started = True
            if admitted and self._admission_controller is not None:
                await self._admission_controller.begin_user(
                    request.session_id or "default"
                )
                admission_started = True
            if interaction_answer:
                params = request.params if isinstance(request.params, dict) else {}
                await self._clear_pending_interaction(
                    request.session_id or "default",
                    str(params.get("request_id") or params.get("interaction_id") or ""),
                )
            if stateless:
                agent = await self._get_stateless_agent(channel_id)
            else:
                prepare_kwargs: dict[str, Any] = {
                    "sync_metadata": not readonly_goal_get
                }
                if agent_execution is not None:
                    prepare_kwargs["agent_execution"] = agent_execution
                mode, sub_mode, agent = await self._prepare_chat_turn(
                    request,
                    channel_id,
                    **prepare_kwargs,
                )
                if not readonly_goal_get:
                    plan_result = await self._plan_controller.ensure_state(
                        request,
                        mode,
                        sub_mode,
                        agent,
                    )
                    control_events = self._control_events(request, plan_result.events)
                    if on_control_event is not None:
                        for event in control_events:
                            await mark_interaction(event)
                            await on_control_event(event)
                    else:
                        for event in control_events:
                            await mark_interaction(event)
                            yield event
            if on_agent_ready is not None:
                ready_result = on_agent_ready(agent)
                if inspect.isawaitable(ready_result):
                    await ready_result
            managed_heartbeat = (
                background
                and not mailbox_turn
                and self._is_single_agent_session_mode(
                    (request.params or {}).get("mode"),
                    work_mode=(request.params or {}).get("work_mode"),
                )
            )
            response_stream = agent.process_message_stream(request)
            try:
                async for chunk in response_stream:
                    event = RuntimeEvent.from_agent_message(
                        chunk,
                        request_id=request.request_id,
                        channel_id=channel_id,
                        session_id=request.session_id,
                        default_agent_ref=request.agent_ref,
                    )
                    # A Heartbeat owns the entire callback, rather than this
                    # nested output stream. Single-agent interaction answers
                    # must still find its execution before the stream ends.
                    if managed_heartbeat:
                        control_id = self._waiting_control_id(event)
                        if (
                            control_id
                            and not self._session_coordinator.record_interaction(
                                request.session_id or "default",
                                request.request_id,
                                control_id,
                            )
                        ):
                            # No live Heartbeat execution owns this question, so
                            # nobody will ever answer it. Say why it vanished
                            # instead of dropping it silently.
                            logger.warning(
                                "[Runtime] dropping heartbeat interaction with no "
                                "live execution: session_id=%s request_id=%s "
                                "control_id=%s",
                                request.session_id or "default",
                                request.request_id,
                                control_id,
                            )
                            continue
                    else:
                        await mark_interaction(event)
                    if (
                        admission_started
                        and not supersede_attempted
                        and self._event_confirms_user_turn(event)
                    ):
                        supersede_attempted = True
                        await self._supersede_bypassed_session_messages(request)
                    yield event
                activity_execution_succeeded = True
            finally:
                close_stream = getattr(response_stream, "aclose", None)
                if callable(close_stream):
                    await close_stream()
        except GeneratorExit as exc:
            generator_exit = exc
            raise
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            plan_error: BaseException | None = None
            admission_error: BaseException | None = None
            end_error: BaseException | None = None
            should_check_plan_exit = (
                agent is not None and not stateless and not readonly_goal_get
            )
            try:
                if should_check_plan_exit:
                    control_events = self._control_events(
                        request,
                        await self._plan_controller.check_post_process_exit(
                            request,
                            agent,
                        ),
                    )
                    if on_control_event is not None:
                        for event in control_events:
                            await mark_interaction(event)
                            await on_control_event(event)
                    elif generator_exit is None:
                        for event in control_events:
                            await mark_interaction(event)
                            yield event
            except BaseException as exc:  # preserve execution/cancellation below
                plan_error = exc
            finally:
                if admission_started and self._admission_controller is not None:
                    try:
                        await self._admission_controller.end_user(
                            request.session_id or "default"
                        )
                    except BaseException as exc:
                        admission_error = exc
                if foreground_started:
                    try:
                        await self._agent_manager.end_foreground_chat()
                    except BaseException as exc:
                        end_error = exc
                if activity_execution_started:
                    self._record_session_execution_finished(
                        request,
                        succeeded=activity_execution_succeeded,
                    )

            if mailbox_turn and (
                not activity_execution_succeeded or plan_error is not None
            ):
                for control_id in pending_mailbox_interactions:
                    await self._clear_pending_interaction(
                        request.session_id or "default", control_id
                    )

            primary_error: BaseException | None = (
                generator_exit or cancellation or error
            )
            if primary_error is not None:
                self._log_suppressed_cleanup_error(
                    "plan post-processing",
                    plan_error,
                    primary_error,
                )
                self._log_suppressed_cleanup_error(
                    "chat admission cleanup",
                    admission_error,
                    primary_error,
                )
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    primary_error,
                )
            elif plan_error is not None:
                self._log_suppressed_cleanup_error(
                    "chat admission cleanup",
                    admission_error,
                    plan_error,
                )
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    plan_error,
                )
                raise plan_error
            elif admission_error is not None:
                self._log_suppressed_cleanup_error(
                    "foreground cleanup",
                    end_error,
                    admission_error,
                )
                raise admission_error
            elif end_error is not None:
                raise end_error
        if cancellation is not None:
            raise cancellation
        if error is not None:
            yield RuntimeEvent.error(
                request_id=request.request_id,
                channel_id=channel_id,
                session_id=request.session_id,
                error=error,
                metadata=request.metadata,
            )

    async def _stream_session_input_started(
        self, request: AgentRequest, owner_channel: str,
    ) -> AsyncIterator[RuntimeEvent]:
        """Borrow the actual owner; an ingress channel is not an Agent identity."""
        from jiuwenswarm.runtime.events import RuntimeEvent
        from jiuwenswarm.runtime.session_input import SessionInputRejectedError

        lookup = getattr(self._agent_manager, "get_agent_for_session_nowait", None)
        agent = lookup(owner_channel, request.session_id) if callable(lookup) else None
        if agent is None:
            raise SessionInputRejectedError(f"session has no active agent: {request.session_id}")
        deliver = getattr(agent, "deliver_session_input", None)
        if not callable(deliver):
            raise SessionInputRejectedError("active agent does not support supplemental input")
        async with aclosing(deliver(request)) as stream:
            async for chunk in stream:
                event = RuntimeEvent.from_agent_message(
                    chunk, request_id=request.request_id, channel_id=request.channel_id,
                    session_id=request.session_id, default_agent_ref=request.agent_ref,
                )
                await self._mark_pending_interaction(event)
                yield event

    async def _deliver_control(
        self,
        request: AgentRequest,
        *,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Resume existing work while keeping later Heartbeats out."""
        session_id = request.session_id or "default"
        request_id = self._control_request_id(request)
        heartbeat_control = (
            self._session_coordinator.control_target_work_kind(
                session_id, request_id
            )
            is SessionWorkKind.HEARTBEAT
        )
        begin_control = getattr(self._admission_controller, "begin_control", None)
        end_control = getattr(self._admission_controller, "end_control", None)
        admitted = callable(begin_control) and callable(end_control)
        if admitted:
            await begin_control(session_id)
        try:
            events = await self._session_coordinator.deliver_control(
                session_id,
                request_id,
                lambda: self._deliver_control_started(
                    request, on_control_event=on_control_event,
                ),
                suspension_key=self._waiting_control_id,
            )
            if not heartbeat_control:
                for event in events:
                    control_id = self._waiting_control_id(event)
                    if control_id and self._session_coordinator.has_control_target(
                        session_id, control_id
                    ):
                        await self._mark_pending_interaction(event)
            return events
        except BaseException:
            if (
                not heartbeat_control
                and self._session_coordinator.has_control_target(
                    session_id, request_id
                )
            ):
                await self._mark_pending_interaction_id(session_id, request_id)
            raise
        finally:
            if admitted:
                await end_control(session_id)

    async def _deliver_control_started(
        self,
        request: AgentRequest,
        *,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Inject control input without opening another Session work turn."""
        from jiuwenswarm.runtime.events import RuntimeEvent

        channel_id = request.channel_id or "default"
        await self._clear_pending_interaction(
            request.session_id or "default",
            self._control_request_id(request),
        )
        lookup = getattr(self._agent_manager, "get_agent_for_session_nowait", None)
        agent = (
            lookup(channel_id, request.session_id or "") if callable(lookup) else None
        )
        if agent is None:
            raise RuntimeError(
                f"session has no active agent: {request.session_id or 'default'}"
            )
        deliver = getattr(agent, "deliver_control_input", None)
        if not callable(deliver):
            raise RuntimeError("active agent does not accept control input")
        params = request.params if isinstance(request.params, dict) else {}
        answers = params.get("answers")
        card_id = (
            answers[0].get("card_id")
            if isinstance(answers, list) and len(answers) == 1
            and isinstance(answers[0], dict) else None
        )
        # This selects transport timing, not permission. Only the adapter's
        # actual handoff acknowledgement can settle the submitted answer.
        forward_permission_ack = (
            params.get("source") == "permission_interrupt"
            and isinstance(card_id, str) and 0 < len(card_id.strip()) <= 128
        )
        events: list[RuntimeEvent] = []
        response_stream = deliver(request)
        try:
            async for chunk in response_stream:
                event = RuntimeEvent.from_agent_message(
                    chunk,
                    request_id=request.request_id,
                    channel_id=channel_id,
                    session_id=request.session_id,
                    default_agent_ref=request.agent_ref,
                )
                matching_ack = False
                if forward_permission_ack and on_control_event is not None:
                    matching_ack = (
                        event.event_type == "runtime.accepted"
                        and event.request_id == request.request_id
                        and event.session_id == request.session_id
                        and event.payload.get("request_id") == request.request_id
                        and event.payload.get("session_id", event.session_id) == request.session_id
                    )
                if matching_ack:
                    await on_control_event(event)
                else:
                    events.append(event)
        finally:
            close_stream = getattr(response_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()
        return events

    async def _stream_control_started(
        self,
        request: AgentRequest,
    ) -> AsyncIterator[RuntimeEvent]:
        """Deliver to the active Agent while forwarding each observation."""
        from jiuwenswarm.runtime.events import RuntimeEvent

        channel_id = request.channel_id or "default"
        await self._clear_pending_interaction(
            request.session_id or "default",
            self._control_request_id(request),
        )
        lookup = getattr(self._agent_manager, "get_agent_for_session_nowait", None)
        agent = lookup(channel_id, request.session_id or "") if callable(lookup) else None
        if agent is None:
            raise RuntimeError(f"session has no active agent: {request.session_id or 'default'}")
        deliver = getattr(agent, "deliver_control_input", None)
        if not callable(deliver):
            raise RuntimeError("active agent does not accept control input")
        response_stream = deliver(request)
        try:
            async for chunk in response_stream:
                event = RuntimeEvent.from_agent_message(
                    chunk,
                    request_id=request.request_id,
                    channel_id=channel_id,
                    session_id=request.session_id,
                    default_agent_ref=request.agent_ref,
                )
                await self._mark_pending_interaction(event)
                yield event
        finally:
            close_stream = getattr(response_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()

    async def cleanup_session(
        self,
        *,
        channel_id: str,
        session_id: str,
        reset_plan_state: bool = True,
    ) -> bool:
        """Release in-memory resources owned by one Runtime session.

        Session cleanup only touches the manager that already exists from
        ``__init__``.  Keep it available while the first ``start`` is still in
        progress so an AgentServer disconnect never has to bypass this public
        API or start new Runtime dependencies merely to release stale state.

        ``reset_plan_state=False`` supports the existing transactional
        ``session.delete`` flow: Runtime work is drained first, while plan
        state remains available for rollback until all downstream deletion
        steps have committed.  Ordinary disconnect and process-CLI cleanup use
        the default and release both resources together.
        """
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        if self._owns_session(session_id):
            result = await self._session_coordinator.close_session(session_id)
            if result.timed_out:
                raise SessionCloseTimeoutError(session_id, result.timed_out)
        cleaned = await self._agent_manager.cleanup_session_runtime(
            channel_id=channel_id,
            session_id=session_id,
        )
        await self._forget_agent_execution_owner(
            channel_id=channel_id,
            session_id=session_id,
        )
        if reset_plan_state:
            self._plan_controller.reset_session(session_id)
        return cleaned

    def begin_chat_request(self, session_id: str, request_id: str) -> None:
        self._pending_chat_requests.setdefault(session_id, set()).add(request_id)

    def end_chat_request(self, session_id: str, request_id: str) -> None:
        requests = self._pending_chat_requests.get(session_id)
        if requests is None:
            return
        requests.discard(request_id)
        if not requests:
            self._pending_chat_requests.pop(session_id, None)

    def is_session_running(self, session_id: str) -> bool:
        """Read current execution state without cancelling work or fencing admission."""
        if getattr(self, "_pending_chat_requests", {}).get(session_id):
            return True
        snapshot = self._session_coordinator.snapshot_session(session_id)
        if snapshot and any(
            not execution.state.terminal for execution in snapshot.executions
        ):
            return True
        from jiuwenswarm.agents.harness.team.team_manager import is_team_session_running

        return is_team_session_running(session_id)

    def has_parked_team_streams(self, session_id: str) -> bool:
        """Whether every pending chat request is parked on a released Team round.

        A Team first-request handler stays alive for the whole persistent
        leader stream; once its round was released it no longer owns team
        work, only the parked response stream.  Lifecycle actions may pass
        such handlers and leave that stream alone.  A request still
        preparing or mid-round has no released-round marker, so mixed states
        keep the Session running.
        """
        requests = getattr(self, "_pending_chat_requests", {}).get(session_id)
        if not requests:
            return False
        from jiuwenswarm.agents.harness.team.team_manager import (
            team_session_has_parked_request,
        )

        return team_session_has_parked_request(session_id, requests)

    @staticmethod
    def is_team_round_finishing(session_id: str) -> bool:
        """Whether a busy Team session is only wrapping up after its swarmflow runs ended.

        ``swarmflow.stop`` and natural workflow completion keep the round
        active while the leader reports the outcome.  Lifecycle actions use
        this to tell the user to retry shortly instead of asking them to stop
        a session that is already ending on its own.
        """
        from jiuwenswarm.agents.harness.team.team_manager import (
            team_round_finishing_after_flow,
        )

        return team_round_finishing_after_flow(session_id)

    async def stop_session_for_archive(
        self, *, channel_id: str, session_id: str
    ) -> None:
        """Drain runtime writers for deletion; archive now only checks state."""
        from jiuwenswarm.server.runtime.session.lifecycle import (
            LifecycleError,
            assert_runtime_owner,
            release_runtime,
        )

        assert_runtime_owner(session_id)
        closed = await self._session_coordinator.close_session(
            session_id, wait_timeout=10
        )
        if closed.timed_out:
            raise LifecycleError("STOP_TIMEOUT", "runtime executions have not stopped")
        await self._agent_manager.release_subagent_runtime_for_session(
            channel_id=channel_id,
            session_id=session_id,
            reason="session_archived",
        )
        await self.cleanup_session(
            channel_id=channel_id, session_id=session_id, reset_plan_state=False
        )
        release_runtime(session_id)

    async def delete_session(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> SessionDeleteResult:
        """Delete one persisted Session through the shared Runtime boundary."""
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        result = await self._session_provisioner.delete_session(
            channel_id=channel_id,
            session_id=session_id,
            quiesce_session=self._quiesce_agent_session_for_delete,
            dispose_session=self._dispose_agent_session_after_resource_release,
        )
        return result

    async def delete_team(
        self,
        *,
        team_name: str,
        channel_id: str = "",
    ) -> TeamDeleteResult:
        """Delete a Team exclusively through the Runtime business boundary."""
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        return await self._session_provisioner.delete_team(
            team_name=team_name,
            channel_id=channel_id,
        )

    async def _quiesce_agent_session_for_delete(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> None:
        del channel_id
        if not self._owns_session(session_id):
            return
        closed = await self._session_coordinator.close_session(
            session_id,
            wait_timeout=10,
        )
        if closed.timed_out:
            from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError

            raise LifecycleError(
                "STOP_TIMEOUT",
                "runtime executions have not stopped",
            )

    async def _dispose_agent_session_after_resource_release(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> None:
        await self._agent_manager.release_subagent_runtime_for_session(
            channel_id=channel_id or None,
            session_id=session_id,
            reason="session_deleted",
        )
        await self.cleanup_session(
            channel_id=channel_id,
            session_id=session_id,
            reset_plan_state=False,
        )

    async def close(self) -> None:
        """Release resources unless a Session provision is unfinished.

        The check is fail-fast rather than a wait or implicit abort: only the
        caller knows whether a two-phase operation must commit or compensate.
        A rejected close leaves the Runtime started and can be retried after the
        caller finalizes every issued provision lease.

        Runtime releases only its non-owning application-resource lease. The
        application composition root remains responsible for shared shutdown.
        """
        async with self._lifecycle_lock:
            if self._closed:
                return
            for prepared in tuple(self._pending_session_provisions):
                self._discard_finalized_session_provision(prepared)
            if self._session_provision_prepares > 0 or self._pending_session_provisions:
                raise RuntimeStateError(
                    "runtime has unfinished session provisions; "
                    "commit or abort them before close"
                )
            cleanup_errors: list[BaseException] = []
            try:
                await self._session_coordinator.close()
            except SessionCloseTimeoutError:
                raise
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await self._session_provisioner.close_background_tasks()
            except BaseException as exc:
                cleanup_errors.append(exc)
            self._activity_executions.clear()
            self._participant_registry.clear_targets()
            try:
                await self._agent_manager.cancel_all_inflight_work("[runtime close] ")
            except (
                BaseException
            ) as exc:  # preserve cancellation until cleanup completes
                cleanup_errors.append(exc)
            if self._resource_lease is not None:
                try:
                    await self._resource_lease.release()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                finally:
                    self._resource_lease = None
            for agent in self._stateless_agents.values():
                cleanup = getattr(agent, "cleanup", None)
                if callable(cleanup):
                    try:
                        await cleanup()
                    except BaseException as exc:
                        cleanup_errors.append(exc)
            self._stateless_agents.clear()
            try:
                await self._agent_manager.cleanup()
            except BaseException as exc:
                cleanup_errors.append(exc)
            async with self._agent_execution_owner_lock:
                self._agent_execution_owners.clear()
            if self._shared_extensions_acquired:
                try:
                    await _release_process_runtime_extensions()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                finally:
                    self._shared_extensions_acquired = False
            if self._shared_dependencies_acquired:
                try:
                    await _release_process_runtime_dependencies()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                finally:
                    self._shared_dependencies_acquired = False
                    self._runner_started = False
                    self._checkpointer_started = False
            self._started = False
            self._closed = True
            if cleanup_errors:
                raise cleanup_errors[0]

    def _require_started(self) -> None:
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        if not self._started:
            raise RuntimeStateError("runtime is not started")

    @staticmethod
    def _chat_turn_methods() -> frozenset[Any]:
        from jiuwenswarm.runtime.request import CHAT_TURN_METHODS

        return CHAT_TURN_METHODS

    @staticmethod
    def _is_interrupt_resume_request(request: AgentRequest) -> bool:
        """Do not re-admit answers that inject into an active interaction.

        ``ask_user`` / permission answers are transported as ``chat.send``
        with an interrupt payload.  The original chat turn still owns the
        session admission while it waits for that answer, so making the
        answer wait for a second user admission deadlocks the interaction and
        blocks Heartbeat indefinitely.
        """
        # Keep this dependency lazy with the request-shape inspection path.
        # pylint: disable-next=import-outside-toplevel
        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            is_interrupt_resume_payload,
        )

        return is_interrupt_resume_payload(request.params)

    @staticmethod
    def _event_confirms_user_turn(event: RuntimeEvent) -> bool:
        """Return whether an Agent response accepted an ordinary user turn."""

        from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES

        return bool(
            event.ok
            and event.event_type not in TERMINAL_ERROR_EVENT_TYPES
        )

    async def _supersede_bypassed_session_messages(
        self,
        request: AgentRequest,
    ) -> None:
        """Resolve stale mailbox waits while this user still owns admission."""

        if request.req_method not in (ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME):
            return
        params = request.params if isinstance(request.params, dict) else {}
        if params.get(SESSION_MESSAGE_INTERNAL_KEY) is not None:
            return
        if self._is_interrupt_resume_request(request):
            return
        service = self._session_message_service
        if service is None:
            return
        target_session_id = str(request.session_id or "").strip()
        if not target_session_id:
            return
        try:
            superseded = await service.supersede_waiting_for_target(target_session_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[SessionMessaging] failed to supersede bypassed messages: "
                "session_id=%s",
                target_session_id,
            )
            return
        if superseded:
            await self.release_session_message_interactions(target_session_id)
            logger.info(
                "[SessionMessaging] user turn superseded %d waiting message(s): "
                "session_id=%s",
                superseded,
                target_session_id,
            )

    async def release_session_message_interactions(
        self, session_id: str, *, request_id: str | None = None
    ) -> None:
        """Release questions whose mailbox turn was superseded or failed."""
        coordinator = getattr(self, "_session_coordinator", None)
        snapshot = coordinator.snapshot_session(session_id) if coordinator else None
        if snapshot is None:
            return
        for execution in snapshot.executions:
            if execution.state is not SessionExecutionState.WAITING_FOR_CONTROL:
                continue
            if (
                (execution.root_work_kind or execution.work_kind)
                is not SessionWorkKind.SESSION_MESSAGE
                or (
                    request_id is not None
                    and (execution.root_request_id or execution.request_id) != request_id
                )
            ):
                continue
            await coordinator.cancel_execution(
                session_id, execution_id=execution.execution_id
            )
            await self._clear_pending_interaction(
                session_id, execution.waiting_control_id
            )

    def _should_admit_interrupt_resume(self, request: AgentRequest) -> bool:
        """Admit a stale answer, but let a live turn inject without waiting.

        A valid interrupt answer normally arrives while the original user turn
        still owns the session admission.  Only that case skips a second
        admission.  If no user work is active, retain the normal admission
        barrier so an expired answer cannot race a Heartbeat run.
        """
        controller = self._admission_controller
        if controller is None:
            return False
        if not callable(getattr(controller, "is_user_active", None)):
            return False
        session_id = request.session_id or "default"
        if bool(controller.is_user_active(session_id)):
            return False
        is_session_message_active = getattr(
            controller, "is_session_message_active", None
        )
        if callable(is_session_message_active) and is_session_message_active(session_id):
            return False
        return True

    @staticmethod
    def _request_targets_team(request: AgentRequest) -> bool:
        """Resolve Team admission before agent execution begins."""
        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.runtime.request import resolve_agent_request_mode

        params = request.params if isinstance(request.params, dict) else {}
        raw_mode = params.get("mode")
        work_mode = params.get("work_mode")
        if not (isinstance(raw_mode, str) and raw_mode.strip()):
            session_id = str(request.session_id or "").strip()
            if session_id:
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )

                try:
                    metadata = get_session_metadata(
                        session_id,
                        cache_bust=True,
                        enable_writeback=False,
                    )
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Runtime admission could not read session %s: %s",
                        session_id,
                        exc,
                    )
                    metadata = {}
                if isinstance(metadata, dict):
                    raw_mode = metadata.get("mode")
                    work_mode = metadata.get("work_mode") or work_mode
        _mode, _sub_mode, canonical = resolve_agent_request_mode(
            raw_mode,
            work_mode=work_mode,
        )
        return is_team_mode(canonical)

    @classmethod
    def uses_session_runtime(
        cls,
        request: AgentRequest,
        *,
        background: bool = False,
    ) -> bool:
        """Return whether the Session Runtime owns this execution."""
        return cls.session_work_kind(request, background=background) is not None

    @classmethod
    def _is_session_input_request(cls, request: AgentRequest) -> bool:
        return (
            request.req_method in cls._chat_turn_methods()
            and resolve_session_input_mode(request.params) is not None
            and not cls._is_interrupt_resume_request(request)
        )

    @staticmethod
    def _starts_goal(request: AgentRequest) -> bool:
        params = request.params if isinstance(request.params, dict) else {}
        return (
            request.req_method is ReqMethod.COMMAND_GOAL
            and str(params.get("action") or "get").strip().lower() in {"set", "resume"}
        )

    @classmethod
    def session_work_kind(
        cls,
        request: AgentRequest,
        *,
        background: bool = False,
    ) -> SessionWorkKind | None:
        """Classify product Session work at the Runtime boundary."""
        if not request.session_id:
            return None
        params = request.params if isinstance(request.params, dict) else {}
        if background:
            return (
                SessionWorkKind.SESSION_MESSAGE
                if request.req_method in cls._chat_turn_methods()
                and isinstance(params.get(SESSION_MESSAGE_INTERNAL_KEY), dict)
                and cls._is_single_agent_session_mode(
                    params.get("mode"), work_mode=params.get("work_mode")
                )
                else None
            )
        if not cls._is_single_agent_session_mode(
            params.get("mode"),
            work_mode=params.get("work_mode"),
        ):
            return None
        if cls._is_interrupt_resume_request(request):
            return SessionWorkKind.CONTROL_INPUT
        if request.req_method is ReqMethod.COMMAND_GOAL:
            action = str(params.get("action") or "get").strip().lower()
            return (
                SessionWorkKind.GOAL_STREAM
                if action in {"set", "resume"}
                else SessionWorkKind.GOAL_CONTROL
            )
        if request.req_method not in cls._chat_turn_methods():
            return None
        if params.get("attach_goal") is True:
            return SessionWorkKind.GOAL_ATTACH
        if resolve_session_input_mode(params) is not None:
            return SessionWorkKind.SESSION_INPUT
        return (
            SessionWorkKind.CHAT_STREAM
            if request.is_stream
            else SessionWorkKind.CHAT_UNARY
        )

    async def _ensure_session_registered(self, request: AgentRequest) -> None:
        """Idempotently adopt direct callers that already own a product ID."""
        session_id = str(request.session_id or "").strip()
        if self._owns_session(session_id):
            return
        await self._register_session(
            session_id=session_id,
            channel_id=request.channel_id or "default",
        )

    @staticmethod
    def _control_request_id(request: AgentRequest) -> str:
        params = request.params if isinstance(request.params, dict) else {}
        return str(params.get("request_id") or request.request_id or "")

    @staticmethod
    def _waiting_control_id(value: object) -> str | None:
        events = value if isinstance(value, (list, tuple)) else (value,)
        for event in events:
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            event_type = getattr(event, "event_type", "")
            key = (
                "request_id"
                if event_type == "chat.ask_user_question"
                else "interaction_id"
                if event_type == "harness.activate_interaction"
                else None
            )
            if key is not None:
                control_id = str(payload.get(key) or "").strip()
                if control_id:
                    return control_id
        return None

    def _activity_target(self, request: AgentRequest) -> SessionLifecycleTarget | None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return None
        target = self._participant_registry.target(session_id)
        if target is not None:
            return target
        from jiuwenswarm.common.mode_matrix import is_team_mode

        mode = str(params.get("mode") or "agent.plan")
        target = SessionLifecycleTarget(
            descriptor=LifecycleSessionDescriptor(
                session_id=session_id,
                channel_id=str(request.channel_id or "default"),
                mode=mode,
                work_mode=str(params.get("work_mode") or "work"),
                project_id=str(params.get("project_id") or ""),
                project_dir=str(params.get("project_dir") or params.get("cwd") or ""),
                user_id=str(params.get("user_id") or ""),
            ),
            kind=(
                SessionKind.TEAM
                if bool(params.get("team")) or is_team_mode(mode)
                else SessionKind.AGENT
            ),
            team_name=str(params.get("team_name") or ""),
        )
        self._participant_registry.remember_target(target)
        return target

    async def _record_session_execution_started(
        self,
        request: AgentRequest,
        *,
        participants: tuple[Any, ...] | None = None,
    ) -> bool:
        """Publish a Runtime execution-start fact to injected participants."""
        if participants is None:
            participants = self._participant_registry.snapshot_activity()
        if not participants:
            return False
        target = self._activity_target(request)
        if target is None:
            return False
        key = (target.descriptor.session_id, str(request.request_id or ""))
        self._activity_executions[key] = (target, participants)
        try:
            from jiuwenswarm.server.runtime.session.session_history import (
                history_exists,
            )

            has_history = history_exists(target.descriptor.session_id)
        except Exception:
            has_history = False
        event = SessionExecutionEvent(
            target=target,
            request_id=key[1],
            has_history=has_history,
        )
        for participant in participants:
            try:
                await participant.execution_started(event)
            except Exception as exc:
                logger.warning(
                    "Runtime activity execution_started failed: session_id=%s error=%s",
                    target.descriptor.session_id,
                    exc,
                )
        return True

    def _record_session_execution_finished(
        self,
        request: AgentRequest,
        *,
        succeeded: bool,
    ) -> None:
        """Publish the paired execution-finish fact exactly once."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return
        key = (session_id, str(request.request_id or ""))
        execution = self._activity_executions.pop(key, None)
        if execution is None:
            return
        target, participants = execution
        event = SessionExecutionFinishedEvent(
            target=target,
            request_id=key[1],
            succeeded=succeeded,
        )
        for participant in participants:
            try:
                participant.execution_finished(event)
            except Exception as exc:
                logger.warning(
                    "Runtime activity execution_finished failed: session_id=%s error=%s",
                    target.descriptor.session_id,
                    exc,
                )

    async def record_session_input_intent(
        self,
        request: AgentRequest,
        *,
        view_id: str = "default-view",
    ) -> str:
        """Publish a transport-neutral user input intent to participants."""
        participants = self._participant_registry.snapshot_activity()
        if not participants:
            return "disabled"
        target = self._activity_target(request)
        if target is None:
            return "disabled"
        try:
            from jiuwenswarm.server.runtime.session.session_history import (
                history_exists,
            )

            has_history = history_exists(target.descriptor.session_id)
        except Exception:
            has_history = False
        event = SessionInputIntentEvent(
            target=target,
            has_history=has_history,
            view_id=view_id,
            intent_id=str(
                (request.params if isinstance(request.params, dict) else {}).get(
                    "intent_id"
                )
                or request.request_id
                or ""
            ),
        )
        results = []
        failures = 0
        for participant in participants:
            try:
                results.append(await participant.session_input_intent(event))
            except Exception as exc:
                failures += 1
                logger.warning("Runtime activity session_input_intent failed: %s", exc)
        if SessionInputIntentDisposition.SCHEDULED in results:
            return "scheduled"
        if results:
            return "not_needed"
        return "failed" if failures else "disabled"

    async def record_session_inactive(self, request: AgentRequest) -> None:
        participants = self._participant_registry.snapshot_activity()
        if not participants:
            return
        target = self._activity_target(request)
        if target is None:
            return
        event = SessionInactiveEvent(target=target)
        for participant in participants:
            try:
                await participant.session_inactive(event)
            except Exception as exc:
                logger.warning("Runtime activity session_inactive failed: %s", exc)

    async def record_session_inactive_by_id(self, session_id: str) -> None:
        """Receive a KVC-neutral inactivity fact from an execution controller."""
        participants = self._participant_registry.snapshot_activity()
        if not participants:
            return
        target = self._participant_registry.target(str(session_id or "").strip())
        if target is None:
            logger.debug(
                "Runtime activity target missing; skip inactive: session_id=%s",
                session_id,
            )
            return
        event = SessionInactiveEvent(target=target)
        for participant in participants:
            try:
                await participant.session_inactive(event)
            except Exception as exc:
                logger.warning("Runtime activity session_inactive failed: %s", exc)

    @staticmethod
    def _is_stateless_method_request(request: AgentRequest) -> bool:
        return request.req_method is not None and request.req_method.value.startswith(
            (
                "skills.",
                "skilldev.",
                "plugins.",
                "symphony.",
                "agent_groups.",
                "agent_templates.",
                "plugin_packages.",
            )
        )

    @staticmethod
    def _is_readonly_goal_get_request(request: AgentRequest) -> bool:
        if request.req_method != ReqMethod.COMMAND_GOAL:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        return str(params.get("action") or "get").strip().lower() == "get"

    async def _get_stateless_agent(self, channel_id: str) -> Any:
        cached = self._agent_manager.get_agent_nowait(
            channel_id=channel_id,
            mode="agent",
        )
        if cached is not None:
            return cached
        agent = self._stateless_agents.get(channel_id)
        if agent is None:
            from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

            agent = JiuWenSwarm()
            self._stateless_agents[channel_id] = agent
        return agent

    async def _ensure_extensions(self) -> None:
        self._shared_extensions_acquired = await _acquire_process_runtime_extensions()

    async def _rollback_start(self) -> None:
        """Undo partially initialized owned dependencies after start failure."""
        cleanup_errors: list[BaseException] = []
        if self._shared_extensions_acquired:
            try:
                await _release_process_runtime_extensions()
            except BaseException as exc:
                cleanup_errors.append(exc)
            finally:
                self._shared_extensions_acquired = False
        if self._shared_dependencies_acquired:
            try:
                await _release_process_runtime_dependencies()
            except BaseException as exc:
                cleanup_errors.append(exc)
            finally:
                self._shared_dependencies_acquired = False
                self._runner_started = False
                self._checkpointer_started = False
        if cleanup_errors:
            raise cleanup_errors[0]

    @staticmethod
    def _control_events(
        request: AgentRequest,
        payloads: list[dict[str, Any]],
    ) -> list[RuntimeEvent]:
        from jiuwenswarm.runtime.events import RuntimeEvent

        return [
            RuntimeEvent.control(
                request_id=request.request_id,
                channel_id=request.channel_id or "default",
                session_id=request.session_id,
                payload=payload,
            )
            for payload in payloads
        ]

    async def _emit_control_events(
        self,
        request: AgentRequest,
        payloads: list[dict[str, Any]],
        *,
        events: list[RuntimeEvent],
        handler: Callable[[RuntimeEvent], Awaitable[None]] | None,
    ) -> None:
        control_events = AgentRuntime._control_events(request, payloads)
        if handler is None:
            for event in control_events:
                await self._mark_pending_interaction(event)
            events.extend(control_events)
            return
        for event in control_events:
            await self._mark_pending_interaction(event)
            await handler(event)

    @staticmethod
    def _log_suppressed_cleanup_error(
        stage: str,
        cleanup_error: BaseException | None,
        primary_error: BaseException,
    ) -> None:
        if cleanup_error is None:
            return
        logger.warning(
            "Runtime %s failed while preserving primary %s: %s",
            stage,
            type(primary_error).__name__,
            cleanup_error,
            exc_info=(
                type(cleanup_error),
                cleanup_error,
                cleanup_error.__traceback__,
            ),
        )

    @staticmethod
    async def _trigger_before_chat_request_hook(request: AgentRequest) -> None:
        if request.req_method not in AgentRuntime._chat_turn_methods():
            return
        from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
        from jiuwenswarm.extensions.hooks_context import AgentServerChatHookContext
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        context = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=(
                request.req_method.value if request.req_method is not None else None
            ),
            params=params,
        )
        await ExtensionRegistry.get_instance().trigger(
            AgentServerHookEvents.BEFORE_CHAT_REQUEST,
            context,
        )


__all__ = ["AgentRuntime", "RuntimeStateError"]
