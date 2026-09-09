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
from typing import TYPE_CHECKING, Any, TypeVar

from jiuwenswarm.runtime.session_provisioner import (
    PreparedSessionProvision,
    RuntimeSessionProvisioner,
    SessionDeleteResult,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionResult,
    SessionProvisionState,
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
                        "Runtime dependency rollback failed while preserving "
                        "%s: %s",
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
                        "Runtime extension rollback failed while preserving "
                        "%s: %s",
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
        self._stateless_agents: dict[str, Any] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._session_provision_prepares = 0
        self._pending_session_provisions: set[
            PreparedSessionProvision[Any]
        ] = set()
        self._started = False
        self._closed = False

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
        return await self._agent_manager.create_session(
            channel_id=channel_id,
            session_id=requested or None,
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
            return await self._session_provisioner.commit_session_provision(
                prepared,
                timing=timing,
                context=context,
            )
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

        return await cancel_request(
            self._agent_manager,
            request,
            allow_create=allow_create,
        )

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
        if admitted and self._is_interrupt_resume_request(request):
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
            response = await agent.process_message(request)
            events.append(
                RuntimeEvent.from_agent_message(
                    response,
                    request_id=request.request_id,
                    channel_id=channel_id,
                    session_id=request.session_id,
                    default_agent_ref=request.agent_ref,
                    default_complete=True,
                )
            )
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
        from jiuwenswarm.common.schema.message import ReqMethod

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
        tracks_kvc_task = (
            is_chat_turn and not background and self._enable_kvc_tracking
        )
        kvc_task_started = False
        kvc_task_succeeded = False
        admitted = (
            is_chat_turn
            and not background
            and not self._request_targets_team(request)
        )
        if admitted and self._is_interrupt_resume_request(request):
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
                            await on_control_event(event)
                    else:
                        for event in control_events:
                            yield event
            if on_agent_ready is not None:
                ready_result = on_agent_ready(agent)
                if inspect.isawaitable(ready_result):
                    await ready_result
            response_stream = agent.process_message_stream(request)
            try:
                async for chunk in response_stream:
                    yield RuntimeEvent.from_agent_message(
                        chunk,
                        request_id=request.request_id,
                        channel_id=channel_id,
                        session_id=request.session_id,
                        default_agent_ref=request.agent_ref,
                    )
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
                            await on_control_event(event)
                    elif generator_exit is None:
                        for event in control_events:
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

            primary_error: BaseException | None = generator_exit or cancellation or error
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
            if (
                self._session_provision_prepares > 0
                or self._pending_session_provisions
            ):
                raise RuntimeStateError(
                    "runtime has unfinished session provisions; "
                    "commit or abort them before close"
                )
            cleanup_errors: list[BaseException] = []
            try:
                await self._agent_manager.cancel_all_inflight_work("[runtime close] ")
            except BaseException as exc:  # preserve cancellation until cleanup completes
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
        from jiuwenswarm.common.schema.message import ReqMethod

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

    @staticmethod
    async def _emit_control_events(
        request: AgentRequest,
        payloads: list[dict[str, Any]],
        *,
        events: list[RuntimeEvent],
        handler: Callable[[RuntimeEvent], Awaitable[None]] | None,
    ) -> None:
        control_events = AgentRuntime._control_events(request, payloads)
        if handler is None:
            events.extend(control_events)
            return
        for event in control_events:
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
