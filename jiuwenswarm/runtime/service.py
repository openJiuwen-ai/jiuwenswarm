# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle owner for the shared JiuwenSwarm agent runtime.

This module deliberately has no transport concerns.  AgentServer and the
process-style CLI both own an ``AgentRuntime`` instance and use its public
operations; WebSocket framing remains in AgentServer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.agent_catalog import (
    AgentCatalogError,
    AgentCatalogInput,
    AgentCatalogResult,
    AgentCatalogScope,
    AgentDescriptor,
    AgentToolsResult,
    get_agent as get_catalog_agent,
    list_agent_tools as build_agent_tool_catalog,
    list_agents as build_agent_catalog,
    resolve_agent_catalog_scope,
)
from jiuwenswarm.runtime.context_compaction import (
    ContextCompactInput,
    ContextCompactResult,
)
from jiuwenswarm.runtime.memory_catalog import (
    MemoryCatalogError,
    MemoryListResult,
    MemoryLocationsResult,
    MemoryScopeInput,
    MemoryStatusResult,
    ResolvedMemoryScope,
    ensure_trusted_project,
    get_memory_locations as build_memory_locations,
    get_memory_status as build_memory_status,
    list_memory_sources as build_memory_source_list,
    resolve_existing_directory,
)
from jiuwenswarm.runtime.mcp_catalog import (
    McpCatalogListInput,
    McpCatalogListResult,
    McpCatalogShowInput,
    McpCatalogShowResult,
    list_mcp_servers as build_mcp_catalog,
    show_mcp_server as build_mcp_detail,
)
from jiuwenswarm.runtime.model_catalog import (
    ModelCatalogError,
    ModelCatalogResult,
    ModelSelectionResult,
    build_model_catalog,
    resolve_model_selection,
)
from jiuwenswarm.runtime.permission_catalog import (
    PermissionCatalogError,
    PermissionSnapshotInput,
    PermissionSnapshotResult,
    read_permission_snapshot,
)
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
    SessionPersistencePolicy,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session_catalog import SessionListResult, SessionSummary
from jiuwenswarm.runtime.session_mutation import session_mutation_lock
from jiuwenswarm.runtime.session_rewind import (
    SessionRewindAction,
    SessionRewindContextPolicy,
    SessionRewindError,
    SessionRewindFile,
    SessionRewindFileError,
    SessionRewindInput,
    SessionRewindListInput,
    SessionRewindListResult,
    SessionRewindResult,
    SessionRewindTurn,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.runtime.plan import PlanModeController
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


def _extract_compact_summary_processor(summary: str) -> str:
    for line in str(summary or "").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "processor":
            return value.strip()
    return ""


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
        enable_kvc_tracking: bool = False,
        session_coordinator: RuntimeSessionCoordinator | None = None,
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
        self._session_provisioner = RuntimeSessionProvisioner(
            agent_manager=self._agent_manager,
            plan_controller=self._plan_controller,
            delete_lifecycle=session_delete_lifecycle,
        )
        self._enable_kvc_tracking = bool(enable_kvc_tracking)
        self._session_coordinator = session_coordinator or RuntimeSessionCoordinator()
        self._stateless_agents: dict[str, Any] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._session_provision_prepares = 0
        self._pending_session_provisions: set[PreparedSessionProvision[Any]] = set()
        self._started = False
        self._closed = False

    @property
    def agent_manager(self) -> AgentManager:
        """Return the single AgentManager owned by this runtime."""
        return self._agent_manager

    @property
    def plan_controller(self) -> PlanModeController:
        return self._plan_controller

    @property
    def session_coordinator(self) -> RuntimeSessionCoordinator:
        return self._session_coordinator

    def set_admission_controller(self, controller: Any | None) -> None:
        """Attach optional host-owned scheduling admission to chat execution."""
        self._admission_controller = controller

    async def _mark_pending_interaction(self, event: RuntimeEvent) -> None:
        if event.event_type != "chat.ask_user_question":
            return
        marker = getattr(
            self._admission_controller,
            "mark_interaction_pending",
            None,
        )
        if callable(marker):
            payload = event.payload if isinstance(event.payload, dict) else {}
            await marker(
                event.session_id or "default",
                str(payload.get("request_id") or ""),
            )

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
        resolved_session_id = await self._agent_manager.create_session(
            channel_id=channel_id,
            session_id=requested or None,
        )
        await self.register_session(
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
            model=str(metadata.get("model") or "").strip(),
        )

    async def list_models(
        self,
        *,
        channel_id: str,
        session_id: str | None = None,
        selected_model: str = "",
    ) -> ModelCatalogResult:
        """Return a credential-free model catalog for a Runtime client."""
        self._require_started()
        normalized_channel_id = str(channel_id or "").strip().lower()
        if not normalized_channel_id:
            raise ModelCatalogError("channel_id is required", code="BAD_REQUEST")
        current_selection = str(selected_model or "").strip()
        target_session_id = str(session_id or "").strip()
        if target_session_id:
            descriptor = await self.describe_session(session_id=target_session_id)
            if descriptor is None:
                raise ModelCatalogError("session not found", code="NOT_FOUND")
            if descriptor.channel_id.strip().lower() != normalized_channel_id:
                raise ModelCatalogError(
                    "session not found",
                    code="NOT_FOUND",
                )
            current_selection = current_selection or descriptor.model

        from jiuwenswarm.common.config import get_default_models

        return build_model_catalog(
            get_default_models(),
            current_selection=current_selection,
        )

    async def select_model(
        self,
        *,
        channel_id: str,
        selection: str,
        session_id: str | None = None,
    ) -> ModelSelectionResult:
        """Validate a model and persist it only to one owned Session."""
        self._require_started()
        normalized_session_id = str(session_id or "").strip()
        catalog = await self.list_models(
            channel_id=channel_id,
            session_id=normalized_session_id or None,
        )
        selected = resolve_model_selection(catalog, selection)
        if normalized_session_id:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                update_session_metadata,
            )

            update_session_metadata(
                session_id=normalized_session_id,
                model=selected.selection_key,
                touch_last_message_at=False,
                cache_bust=True,
                sync_write=True,
            )
        return ModelSelectionResult(
            model=selected,
            session_id=normalized_session_id,
            persisted=bool(normalized_session_id),
        )

    async def list_memory_sources(
        self,
        memory_input: MemoryScopeInput,
    ) -> MemoryListResult:
        """List Memory metadata without starting an index or opening files."""
        scope = await self._resolve_memory_scope(memory_input)
        return await asyncio.to_thread(build_memory_source_list, scope)

    async def get_memory_status(
        self,
        memory_input: MemoryScopeInput,
    ) -> MemoryStatusResult:
        """Read safe configured Memory state without allocating resources."""
        scope = await self._resolve_memory_scope(memory_input)
        return await asyncio.to_thread(build_memory_status, scope)

    async def get_memory_locations(
        self,
        memory_input: MemoryScopeInput,
    ) -> MemoryLocationsResult:
        """Resolve Memory paths without opening programs or creating folders."""
        scope = await self._resolve_memory_scope(memory_input)
        return build_memory_locations(scope)

    async def list_agent_definitions(
        self,
        catalog_input: AgentCatalogInput,
    ) -> AgentCatalogResult:
        """List configured Agent definitions without creating an Agent."""
        scope = await self._resolve_agent_catalog_scope(catalog_input)
        return await asyncio.to_thread(build_agent_catalog, scope)

    async def get_agent_definition(
        self,
        catalog_input: AgentCatalogInput,
        *,
        name: str,
    ) -> AgentDescriptor:
        """Read one active Agent definition through a safe DTO."""
        scope = await self._resolve_agent_catalog_scope(catalog_input)
        return await asyncio.to_thread(get_catalog_agent, scope, name)

    async def list_agent_definition_tools(
        self,
        catalog_input: AgentCatalogInput,
    ) -> AgentToolsResult:
        """List static custom-Agent tool choices without Agent initialization."""
        scope = await self._resolve_agent_catalog_scope(catalog_input)
        return await asyncio.to_thread(build_agent_tool_catalog, scope)

    async def _resolve_agent_catalog_scope(
        self,
        catalog_input: AgentCatalogInput,
    ) -> AgentCatalogScope:
        self._require_started()
        channel_id = str(catalog_input.channel_id or "").strip().lower()
        if not channel_id:
            raise AgentCatalogError("channel_id is required")

        session_id = str(catalog_input.session_id or "").strip()
        descriptor = (
            await self.describe_session(session_id=session_id) if session_id else None
        )
        if session_id and (
            descriptor is None
            or (
                descriptor.channel_id
                and descriptor.channel_id.strip().lower() != channel_id
            )
        ):
            raise AgentCatalogError("session not found", code="NOT_FOUND")

        bound_project = descriptor.project_dir if descriptor is not None else ""
        project_dir = bound_project or catalog_input.project_dir
        trusted_dirs = (bound_project,) if bound_project else catalog_input.trusted_dirs
        return resolve_agent_catalog_scope(
            project_dir,
            trusted_dirs=trusted_dirs,
        )

    async def list_mcp_servers(
        self,
        catalog_input: McpCatalogListInput | None = None,
    ) -> McpCatalogListResult:
        """List safe static MCP configuration without probing MCP servers."""
        self._require_started()
        entries = await asyncio.to_thread(self._load_static_mcp_entries)
        return build_mcp_catalog(entries, catalog_input)

    async def show_mcp_server(
        self,
        catalog_input: McpCatalogShowInput,
    ) -> McpCatalogShowResult:
        """Read one safe static MCP descriptor without querying its tools."""
        self._require_started()
        entries = await asyncio.to_thread(self._load_static_mcp_entries)
        return build_mcp_detail(entries, catalog_input)

    @staticmethod
    def _load_static_mcp_entries() -> list[dict[str, Any]]:
        """Load established MCP stores without resolving secrets or endpoints."""
        from jiuwenswarm.common.config import get_mcp_servers

        entries = [dict(item) for item in get_mcp_servers()]
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import read_mcp_state

            state = read_mcp_state()
            records = state.get("mcp") if isinstance(state, dict) else {}
            records = records if isinstance(records, dict) else {}
        except Exception:  # noqa: BLE001 - state enrichment is best effort
            records = {}
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            record = records.get(name)
            if not isinstance(record, dict):
                continue
            entry["connection_state"] = record.get("state")
            entry["integration_type"] = record.get("integration_type")
        return entries

    async def get_permission_snapshot(
        self,
        snapshot_input: PermissionSnapshotInput,
    ) -> PermissionSnapshotResult:
        """Read effective permission configuration without mutating it."""
        self._require_started()
        channel_id = str(snapshot_input.channel_id or "").strip().lower()
        if not channel_id:
            raise PermissionCatalogError(
                "channel_id is required",
                code="BAD_REQUEST",
            )
        session_id = str(snapshot_input.session_id or "").strip()
        if session_id:
            descriptor = await self.describe_session(session_id=session_id)
            if descriptor is None or (
                descriptor.channel_id
                and descriptor.channel_id.strip().lower() != channel_id
            ):
                raise PermissionCatalogError(
                    "session not found",
                    code="NOT_FOUND",
                )
        return await asyncio.to_thread(read_permission_snapshot, snapshot_input)

    async def _resolve_memory_scope(
        self,
        memory_input: MemoryScopeInput,
    ) -> ResolvedMemoryScope:
        self._require_started()
        channel_id = str(memory_input.channel_id or "").strip().lower()
        if not channel_id:
            raise MemoryCatalogError("channel_id is required")

        session_id = str(memory_input.session_id or "").strip()
        descriptor = (
            await self.describe_session(session_id=session_id) if session_id else None
        )
        if session_id and (
            descriptor is None
            or (
                descriptor.channel_id
                and descriptor.channel_id.strip().lower() != channel_id
            )
        ):
            raise MemoryCatalogError("session not found", code="NOT_FOUND")

        bound_project = descriptor.project_dir if descriptor is not None else ""
        project_dir = resolve_existing_directory(
            bound_project or memory_input.project_dir,
            field_name="project directory",
        )
        if not bound_project:
            ensure_trusted_project(project_dir, memory_input.trusted_dirs)
        mode = str(
            (descriptor.mode if descriptor is not None else "")
            or memory_input.mode
            or "agent.code.normal"
        ).strip()
        return ResolvedMemoryScope(
            channel_id=channel_id,
            session_id=session_id,
            mode=mode,
            project_dir=project_dir,
        )

    async def list_sessions(
        self,
        *,
        channel_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> SessionListResult:
        """Return one channel-owned Session page without transport metadata."""
        self._require_started()
        normalized_channel_id = str(channel_id or "").strip().lower()
        if not normalized_channel_id:
            raise ValueError("channel_id is required")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_all_sessions_metadata,
        )

        metadata_items, total = get_all_sessions_metadata(
            limit=limit,
            offset=offset,
            channel_id=normalized_channel_id,
        )

        def _float_value(value: Any) -> float:
            if isinstance(value, bool):
                return 0.0
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _int_value(value: Any) -> int:
            if isinstance(value, bool):
                return 0
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        sessions = tuple(
            SessionSummary(
                session_id=str(item.get("session_id") or "").strip(),
                channel_id=str(item.get("channel_id") or "").strip(),
                title=str(item.get("title") or "").strip(),
                mode=str(item.get("mode") or "").strip(),
                work_mode=str(item.get("work_mode") or "").strip().lower(),
                project_id=str(item.get("project_id") or "").strip(),
                project_dir=str(item.get("project_dir") or "").strip(),
                model=str(item.get("model") or "").strip(),
                created_at=_float_value(item.get("created_at")),
                last_message_at=_float_value(item.get("last_message_at")),
                message_count=_int_value(item.get("message_count")),
            )
            for item in metadata_items
        )
        return SessionListResult(
            sessions=sessions,
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_rewind_turns(
        self,
        rewind_input: SessionRewindListInput,
    ) -> SessionRewindListResult:
        """List selectable turns without exposing history storage to callers."""
        self._require_started()
        session_id, descriptor, _session_dir = await self._rewind_session_scope(
            channel_id=rewind_input.channel_id,
            session_id=rewind_input.session_id,
            require_existing=False,
        )
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            list_session_turns,
        )

        project_dir = (
            str(rewind_input.project_dir or "").strip()
            or (descriptor.project_dir if descriptor is not None else "")
            or None
        )
        payload = await asyncio.to_thread(
            list_session_turns,
            session_id=session_id,
            project_dir=project_dir,
        )
        raw_turns = payload.get("turns") if isinstance(payload, dict) else []
        turns: list[SessionRewindTurn] = []
        for raw_turn in raw_turns if isinstance(raw_turns, list) else []:
            if not isinstance(raw_turn, dict):
                continue
            stats = raw_turn.get("stats")
            stats = stats if isinstance(stats, dict) else {}
            raw_files = raw_turn.get("files")
            files: list[SessionRewindFile] = []
            for raw_file in raw_files if isinstance(raw_files, list) else []:
                if not isinstance(raw_file, dict):
                    continue
                files.append(
                    SessionRewindFile(
                        path=str(raw_file.get("path") or ""),
                        lines_added=self._safe_int(raw_file.get("linesAdded")),
                        lines_removed=self._safe_int(raw_file.get("linesRemoved")),
                        is_new_file=bool(raw_file.get("isNewFile")),
                    )
                )
            turns.append(
                SessionRewindTurn(
                    turn_index=self._safe_int(raw_turn.get("turn_index")),
                    content_preview=str(raw_turn.get("content_preview") or ""),
                    timestamp=raw_turn.get("timestamp", 0),
                    message_id=str(raw_turn.get("id") or ""),
                    request_id=str(raw_turn.get("request_id") or ""),
                    files_changed=self._safe_int(stats.get("filesChanged")),
                    lines_added=self._safe_int(stats.get("linesAdded")),
                    lines_removed=self._safe_int(stats.get("linesRemoved")),
                    files=tuple(files),
                )
            )
        total = self._safe_int(payload.get("total")) if isinstance(payload, dict) else 0
        return SessionRewindListResult(turns=tuple(turns), total=total)

    async def rewind_session(
        self,
        rewind_input: SessionRewindInput,
    ) -> SessionRewindResult:
        """Apply one durable rewind through the shared Runtime boundary.

        Waiting for another process is cancellable and has no side effects.
        Once the first mutation starts, cancellation is deferred until history,
        files and persisted Agent context reach the same terminal state.
        """
        self._require_started()
        if isinstance(rewind_input.turn_index, bool):
            raise SessionRewindError("turn_index must be integer")
        try:
            turn_index = int(rewind_input.turn_index)
        except (TypeError, ValueError) as exc:
            raise SessionRewindError("turn_index must be integer") from exc
        if turn_index < 1:
            raise SessionRewindError("turn_index must be >= 1")
        if not isinstance(rewind_input.action, SessionRewindAction):
            raise SessionRewindError("unknown rewind action")

        session_id, descriptor, session_dir = await self._rewind_session_scope(
            channel_id=rewind_input.channel_id,
            session_id=rewind_input.session_id,
            require_existing=True,
        )
        if (
            rewind_input.context_policy is SessionRewindContextPolicy.ENSURE_PERSISTED
            and descriptor is not None
            and not self.is_single_agent_session_mode(
                descriptor.mode,
                work_mode=descriptor.work_mode,
            )
        ):
            raise SessionRewindError(
                "session rewind is not supported for this mode",
                code="UNSUPPORTED_MODE",
            )
        async with session_mutation_lock(session_dir):
            resolved_pair: tuple[Any, Any] | None = None
            if (
                rewind_input.require_context
                or rewind_input.context_policy
                is SessionRewindContextPolicy.ENSURE_PERSISTED
            ):
                resolved_pair = await self._resolve_rewind_context_agent(
                    channel_id=rewind_input.channel_id,
                    session_id=session_id,
                    descriptor=descriptor,
                    ensure=(
                        rewind_input.context_policy
                        is SessionRewindContextPolicy.ENSURE_PERSISTED
                    ),
                )
                if rewind_input.require_context and resolved_pair is None:
                    raise RuntimeError("no agent instance available")

            mutation = asyncio.create_task(
                self._apply_session_rewind(
                    rewind_input,
                    session_id=session_id,
                    turn_index=turn_index,
                    descriptor=descriptor,
                    resolved_pair=resolved_pair,
                )
            )
            try:
                return await asyncio.shield(mutation)
            except asyncio.CancelledError:
                # Do not let Ctrl+C or a disconnected transport stop between
                # history truncation and checkpointer persistence.
                current = asyncio.current_task()
                while not mutation.done():
                    if current is not None:
                        current.uncancel()
                    try:
                        await asyncio.shield(mutation)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not mutation.cancelled():
                    try:
                        mutation.result()
                    except BaseException as exc:
                        logger.warning(
                            "Session rewind failed while completing cancelled "
                            "operation %s: %s",
                            rewind_input.operation_id,
                            exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
                raise

    async def _rewind_session_scope(
        self,
        *,
        channel_id: str,
        session_id: str,
        require_existing: bool,
    ) -> tuple[str, SessionDescriptor | None, Any]:
        normalized_channel = str(channel_id or "").strip().lower()
        normalized_session = str(session_id or "").strip()
        if not normalized_channel:
            raise SessionRewindError("channel_id is required")
        if not normalized_session:
            raise SessionRewindError("session_id is required")

        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_history import (
            resolve_session_dir,
        )

        session_dir, invalid_reason = resolve_session_dir(
            normalized_session,
            sessions_root=get_agent_sessions_dir(),
        )
        if session_dir is None:
            raise SessionRewindError(invalid_reason or "invalid session_id")
        descriptor = await self.describe_session(session_id=normalized_session)
        if (
            descriptor is not None
            and descriptor.channel_id
            and descriptor.channel_id.strip().lower() != normalized_channel
        ):
            raise SessionRewindError("session not found", code="NOT_FOUND")
        if require_existing and not session_dir.is_dir():
            raise SessionRewindError("session history not found")
        return normalized_session, descriptor, session_dir

    async def _apply_session_rewind(
        self,
        rewind_input: SessionRewindInput,
        *,
        session_id: str,
        turn_index: int,
        descriptor: SessionDescriptor | None,
        resolved_pair: tuple[Any, Any] | None,
    ) -> SessionRewindResult:
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            compact_partial_session,
            restore_session_files,
            rewind_session,
            rewind_session_context,
        )

        restore_payload: dict[str, Any] = {}
        if rewind_input.action in {
            SessionRewindAction.CONVERSATION_AND_FILES,
            SessionRewindAction.FILES_ONLY,
        }:
            restore_payload = await asyncio.to_thread(
                restore_session_files,
                session_id=session_id,
                turn_index=turn_index,
                project_dir=(
                    descriptor.project_dir
                    if descriptor is not None and descriptor.project_dir
                    else None
                ),
            )
            if rewind_input.action is SessionRewindAction.FILES_ONLY:
                return self._build_rewind_result(
                    rewind_input.action,
                    restore_payload,
                    session_id=session_id,
                    turn_index=turn_index,
                )

        if rewind_input.action is SessionRewindAction.COMPACT_UP_TO:
            rewind_payload = await asyncio.to_thread(
                compact_partial_session,
                session_id=session_id,
                turn_index=turn_index,
                direction="up_to",
                llm_summary=rewind_input.compact_summary,
            )
        else:
            rewind_payload = await asyncio.to_thread(
                rewind_session,
                session_id=session_id,
                turn_index=turn_index,
            )

        pair = resolved_pair
        if pair is None:
            pair = await self._resolve_rewind_context_agent(
                channel_id=rewind_input.channel_id,
                session_id=session_id,
                descriptor=descriptor,
                ensure=False,
            )
        context_ok = False
        if pair is None:
            logger.warning(
                "Runtime rewind has no Agent context for session_id=%s channel_id=%s",
                session_id,
                rewind_input.channel_id,
            )
        else:
            deep_agent, _react_agent = pair
            try:
                context_ok = await rewind_session_context(
                    deep_agent=deep_agent,
                    session_id=session_id,
                    turn_index=turn_index,
                )
            except Exception as exc:  # preserve established partial-success contract
                logger.warning("Runtime rewind context rebuild failed: %s", exc)
            if not context_ok:
                logger.warning(
                    "Runtime rewind committed history with context_rebuilt=false "
                    "for session_id=%s",
                    session_id,
                )

        if rewind_input.action is SessionRewindAction.COMPACT_FROM:
            self._append_rewind_compact_records(
                session_id=session_id,
                channel_id=rewind_input.channel_id,
                turn_index=turn_index,
                summarized_count=rewind_input.summarized_count,
                compact_summary=rewind_input.compact_summary,
            )
            rewind_payload = {
                **rewind_payload,
                "summarized_messages": rewind_input.summarized_count,
            }

        return self._build_rewind_result(
            rewind_input.action,
            rewind_payload,
            session_id=session_id,
            turn_index=turn_index,
            context_rebuilt=context_ok,
            restore_payload=restore_payload,
        )

    async def _resolve_rewind_context_agent(
        self,
        *,
        channel_id: str,
        session_id: str,
        descriptor: SessionDescriptor | None,
        ensure: bool,
    ) -> tuple[Any, Any] | None:
        agent = self._agent_manager.get_agent_for_session_nowait(
            channel_id=channel_id or "default",
            session_id=session_id,
        )
        if agent is None:
            agent = self._agent_manager.get_agent_nowait(
                channel_id=channel_id or "default"
            )
        if agent is None and ensure:
            from jiuwenswarm.runtime.request import resolve_agent_request_mode

            requested_mode = (
                descriptor.mode
                if descriptor is not None and descriptor.mode
                else "agent"
            )
            mode, sub_mode, _canonical = resolve_agent_request_mode(
                requested_mode,
                work_mode=(descriptor.work_mode if descriptor is not None else None),
            )
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id or "default",
                mode="agent" if mode == "auto_harness" else mode,
                project_dir=(
                    descriptor.project_dir if descriptor is not None else None
                ),
                sub_mode=sub_mode,
            )
        if agent is None:
            return None

        deep_agent = None
        adapter = self._rewind_adapter(agent)
        if adapter is not None:
            if getattr(adapter, "_is_session_scoped_adapter", False):
                deep_agent = getattr(adapter, "_instance", None)
            else:
                get_cached = getattr(adapter, "_get_cached_session_adapter", None)
                if callable(get_cached):
                    session_adapter = get_cached(session_id)
                    if session_adapter is not None:
                        deep_agent = getattr(session_adapter, "_instance", None)
        if deep_agent is None:
            deep_agent = await agent.ensure_instance()
        if deep_agent is None:
            return None
        react_agent = getattr(deep_agent, "react_agent", None)
        if react_agent is None:
            return None
        return deep_agent, react_agent

    @staticmethod
    def _rewind_adapter(agent: Any) -> Any:
        for attribute in ("_adapter", "adapter", "_active_adapter"):
            adapter = getattr(agent, attribute, None)
            if adapter is not None:
                return adapter
        return agent

    @staticmethod
    def _build_rewind_result(
        action: SessionRewindAction,
        payload: dict[str, Any],
        *,
        session_id: str,
        turn_index: int,
        context_rebuilt: bool | None = None,
        restore_payload: dict[str, Any] | None = None,
    ) -> SessionRewindResult:
        restore = restore_payload if restore_payload is not None else payload
        raw_errors = restore.get("errors")
        error_items = raw_errors if isinstance(raw_errors, list) else []
        errors = tuple(
            SessionRewindFileError(
                file=str(item.get("file") or ""),
                error=str(item.get("error") or ""),
            )
            for item in error_items
            if isinstance(item, dict)
        )
        return SessionRewindResult(
            action=action,
            session_id=str(payload.get("session_id") or session_id),
            turn_index=AgentRuntime._safe_int(
                payload.get("turn_index"), fallback=turn_index
            ),
            content=(
                str(payload.get("content") or "") if "content" in payload else None
            ),
            content_preview=(
                str(payload.get("content_preview") or "")
                if "content_preview" in payload
                else None
            ),
            remaining_records=(
                AgentRuntime._safe_int(payload.get("remaining_records"))
                if "remaining_records" in payload
                else None
            ),
            removed_records=(
                AgentRuntime._safe_int(payload.get("removed_records"))
                if "removed_records" in payload
                else None
            ),
            context_rebuilt=context_rebuilt,
            restored_files=tuple(
                str(item) for item in restore.get("restored_files", [])
            ),
            deleted_files=tuple(str(item) for item in restore.get("deleted_files", [])),
            restore_errors=errors,
            summarized_messages=(
                AgentRuntime._safe_int(payload.get("summarized_messages"))
                if "summarized_messages" in payload
                else None
            ),
            direction=(
                str(payload.get("direction") or "") if "direction" in payload else None
            ),
        )

    @staticmethod
    def _append_rewind_compact_records(
        *,
        session_id: str,
        channel_id: str,
        turn_index: int,
        summarized_count: int,
        compact_summary: str,
    ) -> None:
        import time
        import uuid

        from jiuwenswarm.server.runtime.session.session_history import (
            append_history_record,
        )

        request_id = str(uuid.uuid4())
        now = time.time()
        metadata = {
            "trigger": "manual_rewind",
            "direction": "from",
            "turn_index": turn_index,
            "summarized_messages": summarized_count,
        }
        append_history_record(
            session_id=session_id,
            request_id=request_id,
            channel_id=channel_id or "tui",
            role="assistant",
            event_type="context.compact_boundary",
            content="Conversation compacted",
            timestamp=now,
            extra={"compact_metadata": metadata},
        )
        append_history_record(
            session_id=session_id,
            request_id=request_id,
            channel_id=channel_id or "tui",
            role="assistant",
            event_type="context.rewind_summary",
            content=f"Summarized {summarized_count} messages from this point.",
            timestamp=now + 0.001,
            extra={"compact_metadata": metadata, "is_compact_summary": True},
        )
        if compact_summary.strip():
            append_history_record(
                session_id=session_id,
                request_id=request_id,
                channel_id=channel_id or "tui",
                role="assistant",
                event_type="context.compact_summary",
                content=compact_summary.strip(),
                timestamp=now + 0.002,
                extra={
                    "compact_metadata": metadata,
                    "is_compact_summary": True,
                    "transcript_only": True,
                },
            )

    @staticmethod
    def _safe_int(value: Any, *, fallback: int = 0) -> int:
        if isinstance(value, bool):
            return fallback
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    async def compact_context(
        self,
        compact_input: ContextCompactInput,
        *,
        on_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> ContextCompactResult:
        """Compact one Session without depending on a transport or Server."""
        self._require_started()

        from openjiuwen.harness.observability import (
            close_agent_run_span,
            open_agent_run_span,
        )

        from jiuwenswarm.agents.harness.agent_observability import (
            sync_agent_observability,
        )
        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.runtime.events import RuntimeEvent
        from jiuwenswarm.runtime.request import resolve_agent_request_mode
        from jiuwenswarm.server.runtime.session.session_history import (
            append_compact_history_records,
        )

        session_id = compact_input.session_id or "default"
        channel_id = compact_input.channel_id or "default"
        requested_mode = compact_input.mode or "agent"
        mode, sub_mode, canonical_mode = resolve_agent_request_mode(requested_mode)
        agent_mode = "agent" if mode == "auto_harness" else mode

        agent = self._agent_manager.get_agent_for_session_nowait(
            channel_id=channel_id,
            session_id=session_id,
        )
        if agent is None:
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=compact_input.project_dir,
                sub_mode=sub_mode,
            )
        if agent is None:
            raise ValueError("Failed to get agent")

        await agent.ensure_instance()
        sync_agent_observability()

        execution_subject = None
        if is_team_mode(canonical_mode):
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_agent = get_team_manager(channel_id).get_team_agent(session_id)
            if team_agent is not None:
                execution_subject = team_agent.observability_execution_subject(
                    session_id
                )

        summary = ""
        run_span = open_agent_run_span(
            session_id=session_id,
            mode=requested_mode,
            request_id=compact_input.request_id,
            run_id=compact_input.request_id,
            turn_id=compact_input.request_id,
            execution_subject=execution_subject,
        )
        try:
            result_data = await agent.compress_context(
                session_id=session_id,
                return_state=True,
            )
            result = result_data.get("result")
            stats = result_data.get("stats")
            state = (
                result_data.get("state")
                if isinstance(result_data.get("state"), dict)
                else {}
            )
            summary = str(
                result_data.get("compact_summary")
                or state.get("compact_summary")
                or result_data.get("summary")
                or ""
            ).strip()
            events: list[RuntimeEvent] = []

            if result == "compressed" and stats:
                before_tokens = stats.get("raw_total_tokens", 0)
                after_tokens = stats.get("total_tokens", 0)
                if before_tokens > 0:
                    rate = round(
                        (before_tokens - after_tokens) / before_tokens * 100,
                        1,
                    )
                else:
                    rate = 0
                stats_summary = (
                    f"\u2713 Context compacted: {after_tokens / 1000:.1f}K/"
                    f"{before_tokens / 1000:.1f}K tokens ({rate:.1f}% saved)"
                )

                if summary:
                    append_compact_history_records(
                        session_id=session_id,
                        request_id=compact_input.request_id,
                        channel_id=channel_id,
                        summary=summary,
                        timestamp=_dt.datetime.now().timestamp(),
                        trigger="manual",
                        stats=stats,
                        mode=requested_mode,
                    )
                    event = RuntimeEvent.control(
                        request_id=compact_input.request_id,
                        channel_id=channel_id,
                        session_id=session_id,
                        payload={
                            **state,
                            "event_type": "context.compression_state",
                            "status": state.get("status") or "completed",
                            "phase": state.get("phase") or "active_compress",
                            "processor": state.get("processor")
                            or _extract_compact_summary_processor(summary),
                            "before": state.get("before") or {"tokens": before_tokens},
                            "after": state.get("after") or {"tokens": after_tokens},
                            "saved": state.get("saved")
                            or {
                                "tokens": before_tokens - after_tokens,
                                "percent": rate,
                            },
                            "summary": stats_summary,
                            "compact_summary": summary,
                        },
                    )
                    events.append(event)
                    if on_event is not None:
                        await on_event(event)

            return ContextCompactResult(
                result=result,
                stats=stats,
                summary=summary,
                events=tuple(events),
            )
        finally:
            close_agent_run_span(
                run_span,
                session_id=session_id,
                output=summary,
            )

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
            if mode is not None and self.is_single_agent_session_mode(
                mode,
                work_mode=work_mode,
            ):
                await self.register_session(
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

    async def register_session(self, *, session_id: str, channel_id: str) -> None:
        """Adopt an existing product Session into this Runtime.

        Product create/switch and direct process callers converge here after
        durable lifecycle work, without reallocating the ID or touching
        metadata.
        """
        if self._closed:
            raise RuntimeStateError("runtime is already closed")
        await self._session_coordinator.register_session(
            session_id,
            channel_id,
            SessionPersistencePolicy.PERSISTENT,
        )

    def owns_session(self, session_id: str | None) -> bool:
        """Return whether the Coordinator owns the current Session generation."""
        snapshot = (
            self._session_coordinator.snapshot_session(session_id)
            if session_id
            else None
        )
        return bool(snapshot and snapshot.state is not RuntimeSessionState.CLOSED)

    @staticmethod
    def is_single_agent_session_mode(
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

    async def prepare_chat_turn(
        self,
        request: AgentRequest,
        channel_id: str,
        *,
        sync_metadata: bool = True,
    ) -> tuple[str, str | None, object]:
        """Resolve session semantics and return this Runtime's selected agent."""
        self._require_started()
        from jiuwenswarm.runtime.request import prepare_chat_turn

        return await prepare_chat_turn(
            self._agent_manager,
            request,
            channel_id,
            sync_metadata=sync_metadata,
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
        if request.session_id and self.owns_session(request.session_id):
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

    async def invoke(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
    ) -> list[RuntimeEvent]:
        """Execute one non-streaming request and return Runtime events."""
        await self.start()
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        token = set_runtime_context(self, self._agent_manager)
        try:
            work_kind = self.session_work_kind(request)
            if work_kind is not None:
                await self._ensure_session_registered(request)
                if self._has_control_target(request):
                    return await self._session_coordinator.deliver_control(
                        request.session_id or "default",
                        self._control_request_id(request),
                        lambda: self._deliver_control_started(request),
                        suspension_key=self._waiting_control_id,
                    )
                return await self._session_coordinator.run_unary(
                    request.session_id or "default",
                    request.request_id,
                    work_kind,
                    lambda: self._invoke_started(
                        request,
                        trigger_hook=trigger_hook,
                        on_control_event=on_control_event,
                    ),
                    suspension_key=self._waiting_control_id,
                )
            return await self._invoke_started(
                request,
                trigger_hook=trigger_hook,
                on_control_event=on_control_event,
            )
        finally:
            reset_runtime_context(token)

    async def _invoke_started(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None,
    ) -> list[RuntimeEvent]:
        from jiuwenswarm.runtime.events import RuntimeEvent

        if trigger_hook:
            await self._trigger_before_chat_request_hook(request)
        channel_id = request.channel_id or "default"
        foreground = request.req_method in self._chat_turn_methods()
        tracks_kvc_task = foreground and self._enable_kvc_tracking
        kvc_task_started = False
        kvc_task_succeeded = False
        admitted = foreground and not self._request_targets_team(request)
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
            if tracks_kvc_task:
                await self._record_kvc_chat_started(request)
                kvc_task_started = True
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
                mode, sub_mode, agent = await self.prepare_chat_turn(
                    request,
                    channel_id,
                    sync_metadata=not readonly_goal_get,
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
            response_event = RuntimeEvent.from_agent_message(
                response,
                request_id=request.request_id,
                channel_id=channel_id,
                session_id=request.session_id,
                default_agent_ref=request.agent_ref,
                default_complete=True,
            )
            await self._mark_pending_interaction(response_event)
            events.append(response_event)
            kvc_task_succeeded = True
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
                if kvc_task_started:
                    self._record_kvc_chat_finished(
                        request,
                        succeeded=kvc_task_succeeded,
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
        return await self.invoke(
            request,
            trigger_hook=trigger_hook,
            on_control_event=on_control_event,
        )

    async def stream(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Callable[[RuntimeEvent], Awaitable[None]] | None = None,
        background: bool = False,
        on_agent_ready: Callable[[Any], Any] | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Execute one request and yield the shared Runtime event stream."""
        await self.start()
        from jiuwenswarm.runtime.context import (
            reset_runtime_context,
            set_runtime_context,
        )

        work_kind = self.session_work_kind(request, background=background)
        if work_kind is not None:
            await self._ensure_session_registered(request)
            if self._has_control_target(request):
                events = await self._session_coordinator.deliver_control(
                    request.session_id or "default",
                    self._control_request_id(request),
                    lambda: self._deliver_control_started(request),
                    suspension_key=self._waiting_control_id,
                )
                for event in events:
                    yield event
                return
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
            )
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
    ) -> AsyncIterator[RuntimeEvent]:
        from jiuwenswarm.runtime.events import RuntimeEvent

        if trigger_hook:
            await self._trigger_before_chat_request_hook(request)
        channel_id = request.channel_id or "default"
        is_chat_turn = request.req_method in self._chat_turn_methods()
        foreground = is_chat_turn and not background
        tracks_kvc_task = is_chat_turn and not background and self._enable_kvc_tracking
        kvc_task_started = False
        kvc_task_succeeded = False
        admitted = (
            is_chat_turn and not background and not self._request_targets_team(request)
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
        try:
            if tracks_kvc_task:
                await self._record_kvc_chat_started(request)
                kvc_task_started = True
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
                mode, sub_mode, agent = await self.prepare_chat_turn(
                    request,
                    channel_id,
                    sync_metadata=not readonly_goal_get,
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
                            await self._mark_pending_interaction(event)
                            await on_control_event(event)
                    else:
                        for event in control_events:
                            await self._mark_pending_interaction(event)
                            yield event
            if on_agent_ready is not None:
                ready_result = on_agent_ready(agent)
                if inspect.isawaitable(ready_result):
                    await ready_result
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
                    await self._mark_pending_interaction(event)
                    yield event
                kvc_task_succeeded = True
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
                            await self._mark_pending_interaction(event)
                            await on_control_event(event)
                    elif generator_exit is None:
                        for event in control_events:
                            await self._mark_pending_interaction(event)
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
                if kvc_task_started:
                    self._record_kvc_chat_finished(
                        request,
                        succeeded=kvc_task_succeeded,
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

    async def _deliver_control_started(
        self,
        request: AgentRequest,
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
        events: list[RuntimeEvent] = []
        response_stream = deliver(request)
        try:
            async for chunk in response_stream:
                events.append(
                    RuntimeEvent.from_agent_message(
                        chunk,
                        request_id=request.request_id,
                        channel_id=channel_id,
                        session_id=request.session_id,
                        default_agent_ref=request.agent_ref,
                    )
                )
        finally:
            close_stream = getattr(response_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()
        return events

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
        if self.owns_session(session_id):
            await self._session_coordinator.close_session(session_id)
        cleaned = await self._agent_manager.cleanup_session_runtime(
            channel_id=channel_id,
            session_id=session_id,
        )
        if reset_plan_state:
            self._plan_controller.reset_session(session_id)
        return cleaned

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
            cleanup_session=self.cleanup_session,
        )
        if result.ok:
            self.commit_session_delete(result)
        return result

    def commit_session_delete(self, result: SessionDeleteResult) -> None:
        """Commit Runtime-owned state after persistent Session deletion."""
        self._session_provisioner.commit_session_delete(result)

    async def close(self) -> None:
        """Release resources unless a Session provision is unfinished.

        The check is fail-fast rather than a wait or implicit abort: only the
        caller knows whether a two-phase operation must commit or compensate.
        A rejected close leaves the Runtime started and can be retried after the
        caller finalizes every issued provision lease.
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
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await self._session_provisioner.close_background_tasks()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await self._agent_manager.cancel_all_inflight_work("[runtime close] ")
            except (
                BaseException
            ) as exc:  # preserve cancellation until cleanup completes
                cleanup_errors.append(exc)
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
        return not bool(controller.is_user_active(request.session_id or "default"))

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
    def session_work_kind(
        cls,
        request: AgentRequest,
        *,
        background: bool = False,
    ) -> SessionWorkKind | None:
        """Classify product Session work at the Runtime boundary."""
        if background or not request.session_id:
            return None
        params = request.params if isinstance(request.params, dict) else {}
        if not cls.is_single_agent_session_mode(
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
        input_mode = (
            str(params.get("input_mode") or params.get("runtime_mode") or "")
            .strip()
            .lower()
        )
        if input_mode in {"follow_up", "steer"}:
            return SessionWorkKind.CONTROL_INPUT
        return (
            SessionWorkKind.CHAT_STREAM
            if request.is_stream
            else SessionWorkKind.CHAT_UNARY
        )

    async def _ensure_session_registered(self, request: AgentRequest) -> None:
        """Idempotently adopt direct callers that already own a product ID."""
        session_id = str(request.session_id or "").strip()
        if self.owns_session(session_id):
            return
        await self.register_session(
            session_id=session_id,
            channel_id=request.channel_id or "default",
        )

    @staticmethod
    def _control_request_id(request: AgentRequest) -> str:
        params = request.params if isinstance(request.params, dict) else {}
        return str(params.get("request_id") or request.request_id or "")

    def _has_control_target(self, request: AgentRequest) -> bool:
        return self._is_interrupt_resume_request(
            request
        ) and self._session_coordinator.has_control_target(
            request.session_id or "default",
            self._control_request_id(request),
        )

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

    async def _record_kvc_chat_started(self, request: AgentRequest) -> None:
        """Record a best-effort KVC task fact without changing chat success."""
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                record_chat_started,
            )

            params = request.params if isinstance(request.params, dict) else {}
            await record_chat_started(
                session_id=str(
                    request.session_id or params.get("session_id") or ""
                ).strip(),
                params=params,
                channel_id=str(request.channel_id or "default"),
            )
        except Exception as exc:  # noqa: BLE001 - optional product hook
            logger.warning(
                "Runtime KVC chat-start hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

    @staticmethod
    def _record_kvc_chat_finished(
        request: AgentRequest,
        *,
        succeeded: bool,
    ) -> None:
        """Close the optional KVC task fact without changing chat success."""
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                record_chat_finished,
            )

            params = request.params if isinstance(request.params, dict) else {}
            record_chat_finished(
                session_id=str(
                    request.session_id or params.get("session_id") or ""
                ).strip(),
                succeeded=succeeded,
            )
        except Exception as exc:  # noqa: BLE001 - optional product hook
            logger.warning(
                "Runtime KVC chat-finish hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

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
