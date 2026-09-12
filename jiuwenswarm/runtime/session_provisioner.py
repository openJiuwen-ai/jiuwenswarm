# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral product Session lifecycle contracts and orchestration.

The provisioner owns the business transaction behind ``session.delete`` and
defines the staged contract used to move create, switch, and fork behind the
same Runtime boundary.  AgentServer remains responsible for connection locks,
view identity, request/wire translation, and response delivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Generic, Protocol, TypeAlias, TypeVar
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jiuwenswarm.runtime.plan import PlanModeController
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

logger = logging.getLogger(__name__)

_LEGACY_WORK_MODE_MISMATCH_SEPARATOR = " " * 37

# Preserve the established same-process serialization for explicit TUI IDs,
# even when more than one Runtime instance exists. Weak values avoid retaining
# one lock for every historical Session after no operation references it.
_EXTERNAL_CREATE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)


def _require_bool(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")


def _require_optional_bool(name: str, value: object) -> None:
    if value is not None:
        _require_bool(name, value)


def _session_fork_error_code(error: ValueError) -> str:
    message = str(error)
    if "not found" in message:
        return "NOT_FOUND"
    if "already exists" in message:
        return "ALREADY_EXISTS"
    return "BAD_REQUEST"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreateInput:
    """Normalized business input for create; contains no transport objects.

    ``requested_session_id`` is data, not authorization.  The future create
    implementation must continue to permit it only for the established TUI
    compatibility path identified by ``channel_id``.
    """

    channel_id: str
    requested_session_id: str | None = None
    previous_session_id: str = ""
    create_token: str = ""
    persist_session: bool = False
    persist_session_supplied: bool = False
    mode: str = "agent"
    previous_mode: str | None = None
    is_swarm: bool = False
    team_hint: bool = False
    project_id: str = ""
    project_dir: str = ""
    cwd: str = ""
    work_mode: str | None = None
    work_mode_explicit: bool | None = None
    title: str = ""
    user_id: str = ""
    model_name: str = ""
    cron_id: str = ""

    def __post_init__(self) -> None:
        _require_bool("persist_session", self.persist_session)
        _require_bool("persist_session_supplied", self.persist_session_supplied)
        _require_bool("is_swarm", self.is_swarm)
        _require_bool("team_hint", self.team_hint)
        _require_optional_bool("work_mode_explicit", self.work_mode_explicit)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSwitchInput:
    """Normalized business input for restoring or switching one Session."""

    channel_id: str
    target_session_id: str
    previous_session_id: str = ""
    mode: str = "agent.plan"
    previous_mode: str | None = None
    team_hint: bool = False

    def __post_init__(self) -> None:
        _require_bool("team_hint", self.team_hint)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionForkInput:
    """Normalized business input for copying one persisted Session."""

    channel_id: str
    source_session_id: str
    target_session_id: str | None = None
    title: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreateResult:
    """Successful create result before any Server wire mapping."""

    channel_id: str
    session_id: str
    project_id: str
    project_dir: str
    work_mode: str
    persist_session: bool
    prewarm_hit: bool
    prewarm_status: str
    created: bool
    canonical_mode: str
    explicit_id_compatibility: bool = False

    def __post_init__(self) -> None:
        _require_bool("persist_session", self.persist_session)
        _require_bool("prewarm_hit", self.prewarm_hit)
        _require_bool("created", self.created)
        _require_bool(
            "explicit_id_compatibility",
            self.explicit_id_compatibility,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSwitchResult:
    """Successful switch result before any Server wire mapping."""

    channel_id: str
    session_id: str
    mode: str
    switched: bool = True

    def __post_init__(self) -> None:
        _require_bool("switched", self.switched)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionForkResult:
    """Successful fork result before any Server wire mapping."""

    channel_id: str
    source_session_id: str
    session_id: str
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDescriptor:
    """Transport-neutral persisted Session identity and routing metadata."""

    session_id: str
    channel_id: str
    mode: str
    work_mode: str
    project_id: str = ""
    project_dir: str = ""


SessionProvisionInput: TypeAlias = (
    SessionCreateInput | SessionSwitchInput | SessionForkInput
)
SessionProvisionResult: TypeAlias = (
    SessionCreateResult | SessionSwitchResult | SessionForkResult
)
_ResultT = TypeVar("_ResultT", bound=SessionProvisionResult)


class SessionProvisionError(RuntimeError):
    """Transport-neutral business failure for a Session provision operation.

    ``code`` deliberately remains optional because established Server errors
    include both coded validation failures and uncoded internal failures.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SessionProvisionState(str, Enum):
    """Observable state of a prepared operation and its terminal decision."""

    PREPARED = "prepared"
    COMMITTING = "committing"
    ABORTING = "aborting"
    COMMITTED = "committed"
    ABORTED = "aborted"


class SessionProvisionStateError(RuntimeError):
    """Raised when a prepared operation is finalized by the wrong owner/order."""


class SessionProvisionCommitTiming(str, Enum):
    """Required commit position relative to delivery of a successful result."""

    BEFORE_RESULT_DELIVERY = "before_result_delivery"
    AFTER_RESULT_DELIVERY = "after_result_delivery"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionProvisionCommitContext:
    """Opaque caller-owned context supplied only while committing a result.

    ``foreground_scope_id`` can carry a Server-owned view scope without
    exposing how it was derived.  It is not Session identity.  The contract
    pins it only while a failed/cancelled commit remains retryable and clears
    it after terminal finalization, whether the commit succeeds or propagates
    an operation-declared terminal failure/cancellation.
    """

    foreground_scope_id: str | None = None


class PreparedSessionProvision(Generic[_ResultT]):
    """Opaque result lease returned by a successful Provisioner prepare phase.

    Callers may inspect ``result`` and ``state`` only.  The owning Provisioner
    serializes commit/abort and keeps operation-specific finalizers private.
    Any foreground scope is supplied only inside an opaque commit context; the
    Runtime never derives or interprets a connection/view identity and keeps
    the opaque scope only while a failed commit remains retryable.
    Finalizers are retryable by default: an exception or cancellation leaves
    the lease in ``COMMITTING`` or ``ABORTING`` for the same decision to be
    retried.  An operation whose commit attempt is explicitly terminal keeps
    the original exception but completes the lease after its commit hook has
    started; this models an established operation that cannot be rolled back.
    Once either finalizer starts, the opposite decision is rejected.  Commit
    retries must use the same normalized context as the first attempt.
    """

    __slots__ = (
        "_abort_hook",
        "_commit_attempt_is_terminal",
        "_commit_context",
        "_commit_hook",
        "_commit_timing",
        "_finalize_lock",
        "_owner_token",
        "_result",
        "_state",
    )

    def __init__(
        self,
        *,
        owner_token: object,
        result: _ResultT,
        commit_timing: SessionProvisionCommitTiming,
        commit_hook: (
            Callable[[SessionProvisionCommitContext], Awaitable[None]] | None
        ) = None,
        abort_hook: Callable[[], Awaitable[None]] | None = None,
        commit_attempt_is_terminal: bool = False,
    ) -> None:
        self._owner_token = owner_token
        self._result = result
        self._commit_timing = commit_timing
        self._commit_context: SessionProvisionCommitContext | None = None
        self._commit_hook = commit_hook
        self._abort_hook = abort_hook
        self._commit_attempt_is_terminal = commit_attempt_is_terminal
        self._state = SessionProvisionState.PREPARED
        self._finalize_lock = asyncio.Lock()

    @property
    def result(self) -> _ResultT:
        """Return the immutable domain result produced during prepare."""
        return self._result

    @property
    def state(self) -> SessionProvisionState:
        """Return the current two-phase state without exposing finalizers."""
        return self._state

    @property
    def commit_timing(self) -> SessionProvisionCommitTiming:
        """Return when an adapter must finalize relative to result delivery."""
        return self._commit_timing

    def _assert_owner(self, owner_token: object) -> None:
        if owner_token is not self._owner_token:
            raise SessionProvisionStateError(
                "prepared session provision belongs to a different provisioner"
            )

    async def commit_for_owner(
        self,
        owner_token: object,
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext,
    ) -> _ResultT:
        """Commit through the capability held by the owning Provisioner."""
        self._assert_owner(owner_token)
        async with self._finalize_lock:
            if timing is not self._commit_timing:
                raise SessionProvisionStateError(
                    "session provision commit timing mismatch: "
                    f"expected {self._commit_timing.value}, got {timing!r}"
                )
            if self._state is SessionProvisionState.COMMITTED:
                return self._result
            if self._state is SessionProvisionState.ABORTED:
                raise SessionProvisionStateError(
                    "cannot commit an aborted session provision"
                )
            if self._state is SessionProvisionState.ABORTING:
                raise SessionProvisionStateError(
                    "cannot commit after abort finalization started"
                )
            if self._state is SessionProvisionState.PREPARED:
                self._commit_context = context
                self._state = SessionProvisionState.COMMITTING
            elif self._commit_context != context:
                raise SessionProvisionStateError(
                    "session provision commit context does not match first attempt"
                )
            if self._commit_hook is not None:
                try:
                    await self._commit_hook(self._commit_context or context)
                except BaseException:
                    if self._commit_attempt_is_terminal:
                        self._state = SessionProvisionState.COMMITTED
                        self._commit_context = None
                        self._commit_hook = None
                        self._abort_hook = None
                    raise
            self._state = SessionProvisionState.COMMITTED
            self._commit_context = None
            self._commit_hook = None
            self._abort_hook = None
            return self._result

    async def abort_for_owner(self, owner_token: object) -> None:
        """Abort through the capability held by the owning Provisioner."""
        self._assert_owner(owner_token)
        async with self._finalize_lock:
            if self._state is SessionProvisionState.ABORTED:
                return
            if self._state is SessionProvisionState.COMMITTED:
                raise SessionProvisionStateError(
                    "cannot abort a committed session provision"
                )
            if self._state is SessionProvisionState.COMMITTING:
                raise SessionProvisionStateError(
                    "cannot abort after commit finalization started"
                )
            if self._state is SessionProvisionState.PREPARED:
                self._state = SessionProvisionState.ABORTING
            if self._abort_hook is not None:
                await self._abort_hook()
            self._state = SessionProvisionState.ABORTED
            self._commit_context = None
            self._commit_hook = None
            self._abort_hook = None


class SessionProvisionerContract(Protocol):
    """Target transport-neutral Provisioner surface for incremental migration.

    Implementations prepare all work required before a successful result can be
    exposed, then return an opaque lease.  Adapters must finalize the lease at
    its declared ``commit_timing``.  If delivery fails, they abort only while
    the lease is still prepared; a before-delivery commit is already terminal.
    """

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> PreparedSessionProvision[SessionCreateResult]:
        """Prepare create without constructing a transport response.

        Team ownership preparation completes before this returns.  The lease
        declares ``AFTER_RESULT_DELIVERY`` so create's KVC dispatch remains
        after the successful response.
        """
        ...

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> PreparedSessionProvision[SessionSwitchResult]:
        """Prepare switch without acquiring a connection-scoped lock.

        Team ownership preparation completes before this returns.  The lease
        declares ``BEFORE_RESULT_DELIVERY`` so foreground state is committed
        before the successful response, as in the established handler.
        """
        ...

    async def prepare_session_fork(
        self,
        provision_input: SessionForkInput,
    ) -> PreparedSessionProvision[SessionForkResult]:
        """Prepare a fork through the shared Runtime lifecycle.

        The lease declares ``BEFORE_RESULT_DELIVERY``; fork has no required
        post-response side effect.
        """
        ...

    async def commit_session_provision(
        self,
        prepared: PreparedSessionProvision[_ResultT],
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext | None = None,
    ) -> _ResultT:
        """Finalize one prepared operation at its declared delivery boundary."""
        ...

    async def abort_session_provision(
        self,
        prepared: PreparedSessionProvision[_ResultT],
    ) -> None:
        """Abort one prepared operation before it is committed."""
        ...


class SessionDeleteLifecycle(Protocol):
    """Optional Runtime capability that participates in Session deletion."""

    async def begin_session_delete(self, session_id: str) -> None:
        """Quiesce dependent work before destructive deletion."""
        ...

    async def abort_session_delete(
        self,
        session_id: str,
        *,
        channel_id: str = "",
    ) -> None:
        """Restore dependent work after deletion fails."""
        ...

    async def commit_session_delete(self, session_id: str) -> None:
        """Apply dependent deletion policy after the Session is gone."""
        ...


@dataclass(frozen=True, slots=True)
class SessionDeleteResult:
    """Transport-independent result of one product Session deletion."""

    ok: bool
    session_id: str
    channel_id: str | None = None
    is_team: bool = False
    team_name: str = ""
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def failure(
        cls,
        session_id: str,
        *,
        code: str,
        message: str,
    ) -> SessionDeleteResult:
        return cls(
            ok=False,
            session_id=session_id,
            error_code=code,
            error_message=message,
        )


class RuntimeSessionProvisioner:
    """Coordinate transport-neutral Session lifecycle work for one Runtime."""

    def __init__(
        self,
        *,
        agent_manager: AgentManager,
        plan_controller: PlanModeController,
        delete_lifecycle: SessionDeleteLifecycle | None = None,
    ) -> None:
        self._agent_manager = agent_manager
        self._plan_controller = plan_controller
        self._delete_lifecycle = delete_lifecycle
        self._provision_owner_token = object()
        self._external_create_locks = _EXTERNAL_CREATE_LOCKS
        self._background_create_kvc_tasks: set[asyncio.Task[None]] = set()

    def set_delete_lifecycle(
        self,
        lifecycle: SessionDeleteLifecycle | None,
    ) -> None:
        """Replace the optional lifecycle participant owned by the host."""
        self._delete_lifecycle = lifecycle

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> PreparedSessionProvision[SessionCreateResult]:
        """Prepare one Session create without transport or response objects.

        Metadata and Team ownership are visible before the result is exposed.
        KVC foreground dispatch is retained as an after-delivery finalizer.
        """
        channel_id = str(provision_input.channel_id or "").strip() or "default"
        requested_session_id = str(provision_input.requested_session_id or "").strip()
        previous_session_id = str(provision_input.previous_session_id or "").strip()
        explicit_tui_session = bool(
            requested_session_id and channel_id.lower() == "tui"
        )
        if requested_session_id and not explicit_tui_session:
            raise SessionProvisionError(
                "session.create no longer accepts session_id; "
                "use session.switch to restore"
            )

        external_lock: asyncio.Lock | None = None
        external_lock_acquired = False
        claimed_session_id: str | None = None
        try:
            from jiuwenswarm.server.runtime.session.session_history import (
                is_valid_session_id,
            )
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
                init_session_metadata,
            )

            if explicit_tui_session:
                if not is_valid_session_id(requested_session_id):
                    raise SessionProvisionError("invalid session_id")
                external_lock = self._external_create_locks.setdefault(
                    requested_session_id,
                    asyncio.Lock(),
                )
                await external_lock.acquire()
                external_lock_acquired = True

            params: dict[str, object] = {
                "mode": provision_input.mode,
                "project_id": provision_input.project_id,
                "project_dir": provision_input.project_dir,
                "cwd": provision_input.cwd,
                "work_mode": provision_input.work_mode,
                "title": provision_input.title,
                "user_id": provision_input.user_id,
                "model_name": provision_input.model_name,
                "cron_id": provision_input.cron_id,
                "is_swarm": provision_input.is_swarm,
                "team": provision_input.team_hint,
                "previous_mode": provision_input.previous_mode,
            }
            existing_metadata = (
                get_session_metadata(requested_session_id)
                if explicit_tui_session
                else {}
            )
            persist_session = provision_input.persist_session
            if existing_metadata:
                existing_channel = (
                    str(existing_metadata.get("channel_id") or "").strip().lower()
                )
                if existing_channel not in {"", "tui"}:
                    raise SessionProvisionError(
                        "session_id is already owned by another channel"
                    )
                stored_persist = existing_metadata.get("persist_session") is True
                if (
                    provision_input.persist_session_supplied
                    and persist_session != stored_persist
                ):
                    raise SessionProvisionError(
                        "persist_session is immutable after session creation",
                        code="CONFLICT",
                    )
                persist_session = stored_persist
                for field in ("project_id", "project_dir", "work_mode", "mode"):
                    value = existing_metadata.get(field)
                    if isinstance(value, str) and value.strip():
                        params[field] = value.strip()
            elif explicit_tui_session and not self._uses_projectless_workspace(
                params, channel_id
            ):
                from jiuwenswarm.server.runtime.session.project_store import (
                    find_or_create_code_project_for_tui_params,
                )

                project = find_or_create_code_project_for_tui_params(params)
                if project is not None:
                    params["project_id"] = project.project_id
                    params["project_dir"] = project.project_dir
                    params["work_mode"] = project.work_mode
            elif channel_id.lower() == "tui" and not self._uses_projectless_workspace(
                params, channel_id
            ):
                from jiuwenswarm.server.runtime.session.project_store import (
                    find_or_create_code_project_for_tui_params,
                )

                candidate_dir = str(
                    params.get("project_dir") or params.get("cwd") or ""
                ).strip()
                if not str(params.get("project_id") or "").strip() and candidate_dir:
                    project = find_or_create_code_project_for_tui_params(params)
                    if project is not None:
                        params["project_id"] = project.project_id
                        params["project_dir"] = project.project_dir
                        params["work_mode"] = project.work_mode

            from jiuwenswarm.server.runtime.session.work_mode import (
                resolve_session_work_mode_params,
            )

            binding = resolve_session_work_mode_params(params, channel_id=channel_id)
            if binding.error:
                raise SessionProvisionError(binding.error, code=binding.code)

            from jiuwenswarm.common.work_mode import (
                DEFAULT_WEB_WORK_MODE,
                is_default_project_id,
            )
            from jiuwenswarm.server.runtime.session import project_store

            project_id, project_dir, project_error, project_code = (
                project_store.resolve_session_project_binding(
                    binding.project_id,
                    binding.project_dir,
                )
            )
            if project_error:
                raise SessionProvisionError(project_error, code=project_code)

            has_explicit_work_mode = (
                provision_input.work_mode_explicit
                if provision_input.work_mode_explicit is not None
                else binding.has_explicit_work_mode
            )
            if not is_default_project_id(project_id):
                project = project_store.get_project_by_id(
                    project_id,
                    cache_bust=True,
                )
                if project is None:
                    raise SessionProvisionError(
                        f"project not found: {project_id}",
                        code="NOT_FOUND",
                    )
                project_work_mode = project.work_mode or DEFAULT_WEB_WORK_MODE
                if has_explicit_work_mode and project_work_mode != binding.work_mode:
                    raise SessionProvisionError(
                        "work_mode mismatch: project is "
                        f"'{project_work_mode}'"
                        f"{_LEGACY_WORK_MODE_MISMATCH_SEPARATOR}"
                        "but request specified "
                        f"'{binding.work_mode}'",
                        code="BAD_REQUEST",
                    )
                final_work_mode = project_work_mode
            else:
                final_work_mode = binding.work_mode

            from jiuwenswarm.common.mode_matrix import resolve_request_mode
            from jiuwenswarm.runtime.request import resolve_agent_request_mode

            params.update(
                {
                    "project_id": project_id,
                    "project_dir": project_dir,
                    "work_mode": final_work_mode,
                }
            )
            resolved = resolve_request_mode(
                params,
                resolve_agent_request_mode,
                work_mode=final_work_mode,
            )
            canonical_mode = resolved.canonical_mode
            is_swarm = provision_input.is_swarm or resolved.is_team
            prewarm_eligible = (
                not is_swarm
                and canonical_mode
                in {
                    "agent",
                    "code",
                    "code.normal",
                    "agent.work.normal",
                    "agent.code.normal",
                }
                and self._is_prewarm_model_eligible(provision_input.model_name)
            )

            if explicit_tui_session:
                session_id = requested_session_id
                prewarm_hit = False
                prewarm_status = "bypassed"
            else:
                create_token = str(provision_input.create_token or "").strip()
                if not create_token:
                    raise SessionProvisionError("create_token is required")
                claim = await self._agent_manager.claim_prewarmed_session(
                    channel_id=channel_id,
                    project_id=project_id,
                    project_dir=project_dir,
                    work_mode=final_work_mode,
                    is_swarm=is_swarm,
                    persist_session=persist_session,
                    prewarm_eligible=prewarm_eligible,
                    create_token=create_token,
                )
                session_id = claim.session_id
                claimed_session_id = session_id
                prewarm_hit = claim.prewarm_hit
                prewarm_status = claim.prewarm_status

            from jiuwenswarm.common.utils import get_agent_sessions_dir

            metadata_exists = (
                get_agent_sessions_dir() / session_id / "metadata.json"
            ).is_file()
            if metadata_exists and not explicit_tui_session:
                self._agent_manager.activate_session_prewarm(session_id)
                stored = get_session_metadata(session_id)
                result = SessionCreateResult(
                    channel_id=channel_id,
                    session_id=session_id,
                    project_id=project_id,
                    project_dir=project_dir,
                    work_mode=final_work_mode,
                    persist_session=bool(stored.get("persist_session", False)),
                    prewarm_hit=prewarm_hit,
                    prewarm_status=prewarm_status,
                    created=False,
                    canonical_mode=canonical_mode,
                )
                return self._stage_create_result(
                    result,
                    claimed_session_id=claimed_session_id,
                    external_lock=external_lock,
                )

            session_created = not metadata_exists
            if session_created:
                channel_metadata = None
                if channel_id.lower() == "tui":
                    workspace = str(provision_input.cwd or project_dir or "").strip()
                    if workspace and (
                        not self._uses_projectless_workspace(params, channel_id)
                        or project_dir
                    ):
                        channel_metadata = {
                            "cwd": workspace,
                            "project_dir": project_dir or workspace,
                        }
                init_session_metadata(
                    session_id=session_id,
                    channel_id=channel_id,
                    user_id=str(provision_input.user_id or "").strip(),
                    title=provision_input.title,
                    mode=canonical_mode,
                    project_dir=project_dir,
                    project_id=project_id,
                    persist_session=persist_session,
                    work_mode=final_work_mode,
                    model=str(provision_input.model_name or "").strip(),
                    cron_id=str(provision_input.cron_id or "").strip(),
                    channel_metadata=channel_metadata,
                )
                if not explicit_tui_session:
                    self._agent_manager.activate_session_prewarm(session_id)

            switch_context, dispatch_signals = await self._prepare_create_owner(
                channel_id=channel_id,
                session_id=session_id,
                previous_session_id=previous_session_id,
                params={**params, "mode": canonical_mode},
            )
            result = SessionCreateResult(
                channel_id=channel_id,
                session_id=session_id,
                project_id=project_id,
                project_dir=project_dir,
                work_mode=final_work_mode,
                persist_session=persist_session,
                prewarm_hit=prewarm_hit,
                prewarm_status=prewarm_status,
                created=session_created,
                canonical_mode=canonical_mode,
                explicit_id_compatibility=explicit_tui_session,
            )
            return self._stage_create_result(
                result,
                claimed_session_id=claimed_session_id,
                external_lock=external_lock,
                switch_context=switch_context,
                dispatch_signals=dispatch_signals,
                previous_session_id=previous_session_id,
            )
        except BaseException as primary_error:
            try:
                if claimed_session_id is not None:
                    await self._agent_manager.release_session_prewarm_claim(
                        claimed_session_id
                    )
            except BaseException as cleanup_error:
                logger.warning(
                    "Session create prepare compensation failed while preserving "
                    "%s: session_id=%s error=%s",
                    type(primary_error).__name__,
                    claimed_session_id,
                    cleanup_error,
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )
            finally:
                if external_lock_acquired and external_lock is not None:
                    external_lock.release()
            raise

    def _stage_create_result(
        self,
        result: SessionCreateResult,
        *,
        claimed_session_id: str | None,
        external_lock: asyncio.Lock | None,
        switch_context: object | None = None,
        dispatch_signals: Callable[..., Awaitable[None]] | None = None,
        previous_session_id: str = "",
    ) -> PreparedSessionProvision[SessionCreateResult]:
        finalized = False

        async def release_resources() -> None:
            nonlocal finalized
            if finalized:
                return
            if claimed_session_id is not None:
                await self._agent_manager.release_session_prewarm_claim(
                    claimed_session_id
                )
            if external_lock is not None and external_lock.locked():
                external_lock.release()
            finalized = True

        async def commit_create(
            context: SessionProvisionCommitContext,
        ) -> None:
            try:
                if switch_context is not None and dispatch_signals is not None:
                    task = asyncio.create_task(
                        dispatch_signals(
                            context=switch_context,
                            channel_id=result.channel_id,
                            target_session_id=result.session_id,
                            previous_session_id=previous_session_id,
                            view_id=(context.foreground_scope_id or "default-view"),
                        ),
                        name=f"session-create-kvc-{result.session_id}",
                    )
                    self._background_create_kvc_tasks.add(task)
                    task.add_done_callback(self._background_create_kvc_tasks.discard)
                    task.add_done_callback(self._log_create_kvc_failure)
            finally:
                if external_lock is not None and external_lock.locked():
                    external_lock.release()

        return self._stage_session_provision(
            result,
            commit_timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
            commit_hook=commit_create,
            abort_hook=release_resources,
            commit_attempt_is_terminal=True,
        )

    @staticmethod
    def _log_create_kvc_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(
                "Session create KVC dispatch failed after result delivery: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def close_background_tasks(self) -> None:
        """Cancel and drain Runtime-owned post-create background work."""
        tasks = tuple(self._background_create_kvc_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _prepare_create_owner(
        self,
        *,
        channel_id: str,
        session_id: str,
        previous_session_id: str,
        params: dict[str, object],
    ) -> tuple[object | None, Callable[..., Awaitable[None]] | None]:
        switch_context = None
        dispatch_signals = None
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                dispatch_session_switch_signals,
                resolve_session_switch_context,
            )

            switch_context = resolve_session_switch_context(
                target_session_id=session_id,
                previous_session_id=previous_session_id,
                params=params,
            )
            dispatch_signals = dispatch_session_switch_signals
        except Exception as exc:
            logger.warning(
                "Session create KVC context unavailable; preserving product "
                "lifecycle: session_id=%s error=%s",
                session_id,
                exc,
            )

        from jiuwenswarm.common.mode_matrix import is_team_mode

        if switch_context is not None:
            target_is_team = bool(getattr(switch_context, "target_is_team", False))
            previous_is_team = bool(getattr(switch_context, "previous_is_team", False))
        else:
            target_is_team = bool(params.get("team")) or is_team_mode(
                params.get("mode")
            )
            # Match the established Server fallback: without a resolved KVC
            # context, previous Session ownership is unknown and must not be
            # inferred from a caller-supplied mode hint.
            previous_is_team = False
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            await get_team_manager(channel_id).prepare_session_switch(
                session_id,
                previous_session_id=(previous_session_id if previous_is_team else None),
                reason="session.create switch: ",
            )
        return switch_context, dispatch_signals

    @staticmethod
    def _uses_projectless_workspace(
        params: dict[str, object],
        channel_id: str,
    ) -> bool:
        for key in ("project_dir", "cwd"):
            value = params.get(key)
            if isinstance(value, (str, os.PathLike)) and str(value).strip():
                return False
        from jiuwenswarm.server.runtime.session.work_mode import (
            default_work_mode_for_channel,
        )
        from jiuwenswarm.runtime.request import resolve_agent_request_mode

        work_mode = str(params.get("work_mode") or "").strip().lower()
        if work_mode not in {"code", "work"}:
            work_mode = default_work_mode_for_channel(channel_id)
        manager_mode, _, _ = resolve_agent_request_mode(
            params.get("mode", "agent"),
            work_mode=work_mode,
        )
        return manager_mode in {"agent", "code"}

    @staticmethod
    def _is_prewarm_model_eligible(model_name: str) -> bool:
        requested = str(model_name or "").strip()
        if not requested:
            return True
        from jiuwenswarm.common.config import get_config, get_default_models
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            is_skill_retrieval_enabled,
        )

        config = get_config()
        if not is_skill_retrieval_enabled(config):
            return True
        first_identifiers: set[str] | None = None
        selected_identifiers: set[str] | None = None
        name_counts: dict[str, int] = {}
        for entry in get_default_models(config):
            if not isinstance(entry, dict):
                continue
            client_config = entry.get("model_client_config")
            if not isinstance(client_config, dict):
                continue
            name = str(client_config.get("model_name") or "").strip()
            if not name:
                continue
            occurrence = name_counts.get(name, 0)
            name_counts[name] = occurrence + 1
            identifiers = {name, f"{name}#{occurrence}"}
            alias = str(entry.get("alias") or "").strip()
            if alias:
                identifiers.add(alias)
            if first_identifiers is None:
                first_identifiers = identifiers
            if selected_identifiers is None and entry.get("is_default") is True:
                selected_identifiers = identifiers
        return requested in (selected_identifiers or first_identifiers or set())

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> PreparedSessionProvision[SessionSwitchResult]:
        """Prepare one product Session switch without transport state.

        Team ownership is prepared immediately.  The optional foreground/KVC
        transition is retained as a Runtime-owned commit hook so callers can
        apply it at the required before-result delivery boundary.
        """
        channel_id = str(provision_input.channel_id or "").strip() or "default"
        target_session_id = str(
            provision_input.target_session_id or ""
        ).strip()
        previous_session_id = str(
            provision_input.previous_session_id or ""
        ).strip()
        if not target_session_id:
            raise SessionProvisionError(
                "session_id is required",
                code="BAD_REQUEST",
            )

        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.runtime.request import resolve_agent_request_mode

        _, _, resolved_mode = resolve_agent_request_mode(provision_input.mode)
        target_is_team = provision_input.team_hint or is_team_mode(
            provision_input.mode
        )
        switch_context = None
        dispatch_signals = None
        switch_params = {
            "mode": provision_input.mode,
            "previous_mode": provision_input.previous_mode,
            "team": provision_input.team_hint,
        }
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                dispatch_session_switch_signals,
                resolve_session_switch_context,
            )

            switch_context = resolve_session_switch_context(
                target_session_id=target_session_id,
                previous_session_id=previous_session_id,
                params=switch_params,
            )
            target_is_team = switch_context.target_is_team
            resolved_mode = switch_context.resolved_mode
            dispatch_signals = dispatch_session_switch_signals
        except Exception as exc:
            logger.warning(
                "Session switch KVC context unavailable; preserving product "
                "lifecycle: target_session_id=%s error=%s",
                target_session_id,
                exc,
            )

        previous_is_team = bool(
            switch_context and switch_context.previous_is_team
        )
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_manager = get_team_manager(channel_id)
            await team_manager.prepare_session_switch(
                target_session_id,
                previous_session_id=(
                    previous_session_id if previous_is_team else None
                ),
                reason="session.switch: ",
            )

        async def commit_switch(
            context: SessionProvisionCommitContext,
        ) -> None:
            if switch_context is None or dispatch_signals is None:
                return
            await dispatch_signals(
                context=switch_context,
                channel_id=channel_id,
                target_session_id=target_session_id,
                previous_session_id=previous_session_id,
                view_id=context.foreground_scope_id or "default-view",
            )

        result = SessionSwitchResult(
            channel_id=channel_id,
            session_id=target_session_id,
            mode=resolved_mode,
        )
        return self._stage_session_provision(
            result,
            commit_timing=(
                SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
            ),
            commit_hook=commit_switch,
            commit_attempt_is_terminal=True,
        )

    async def prepare_session_fork(
        self,
        provision_input: SessionForkInput,
    ) -> PreparedSessionProvision[SessionForkResult]:
        """Copy one Session without depending on a Server or transport.

        Persistent history and metadata are copied first, followed by the
        established best-effort in-memory context and checkpoint-state copy.
        The returned lease commits before result delivery and has no deferred
        transport-side work.
        """
        source_session_id = str(provision_input.source_session_id or "").strip()
        target_session_id = str(provision_input.target_session_id or "").strip()
        channel_id = provision_input.channel_id or "default"
        title = str(provision_input.title or "").strip()

        if not source_session_id:
            raise SessionProvisionError(
                "source_session_id is required",
                code="BAD_REQUEST",
            )

        try:
            if not target_session_id:
                target_session_id = await self._agent_manager.create_session(
                    channel_id=channel_id,
                    session_id=None,
                )

            from jiuwenswarm.agents.harness.common.session_ops_service import (
                copy_session_context,
                copy_session_state,
                fork_session,
            )

            fork_result = fork_session(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                title=title,
                channel_id=channel_id,
            )

            agent = self._agent_manager.get_agent_nowait(channel_id)
            deep_agent = None
            if agent is not None:
                deep_agent = await agent.ensure_instance()
                await copy_session_context(
                    deep_agent,
                    source_session_id,
                    target_session_id,
                )
            else:
                logger.warning(
                    "session.fork: no agent for channel %s; "
                    "in-memory context copy skipped",
                    channel_id,
                )

            from openjiuwen.core.single_agent.schema.agent_card import AgentCard

            await copy_session_state(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                card=(
                    deep_agent.card
                    if deep_agent is not None
                    else AgentCard(id="jiuwenswarm", name="jiuwenswarm")
                ),
                deep_agent=deep_agent,
            )
        except ValueError as error:
            raise SessionProvisionError(
                str(error),
                code=_session_fork_error_code(error),
            ) from error

        result = SessionForkResult(
            channel_id=channel_id,
            source_session_id=str(
                fork_result.get("source_session_id") or source_session_id
            ),
            session_id=str(fork_result.get("session_id") or target_session_id),
            title=str(fork_result.get("title") or ""),
        )
        return self._stage_session_provision(
            result,
            commit_timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )

    def _stage_session_provision(
        self,
        result: _ResultT,
        *,
        commit_timing: SessionProvisionCommitTiming,
        commit_hook: (
            Callable[[SessionProvisionCommitContext], Awaitable[None]] | None
        ) = None,
        abort_hook: Callable[[], Awaitable[None]] | None = None,
        commit_attempt_is_terminal: bool = False,
    ) -> PreparedSessionProvision[_ResultT]:
        """Build an owned lease after an operation-specific prepare succeeds.

        This factory is intentionally private.  Future ``prepare_session_*``
        methods register Runtime-owned finalizers here; transports receive only
        the opaque lease and cannot inject executable callbacks.  A finalizer
        must be idempotent and retry-safe unless the operation explicitly
        models an irreversible, terminal commit attempt.
        """
        return PreparedSessionProvision(
            owner_token=self._provision_owner_token,
            result=result,
            commit_timing=commit_timing,
            commit_hook=commit_hook,
            abort_hook=abort_hook,
            commit_attempt_is_terminal=commit_attempt_is_terminal,
        )

    async def commit_session_provision(
        self,
        prepared: PreparedSessionProvision[_ResultT],
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext | None = None,
    ) -> _ResultT:
        """Commit one prepared operation once at its declared delivery point.

        The caller still owns any connection/view identity.  Runtime receives
        only an optional opaque foreground scope and neither derives nor keeps
        its transport meaning.
        """
        return await prepared.commit_for_owner(
            self._provision_owner_token,
            timing=timing,
            context=context or SessionProvisionCommitContext(),
        )

    async def abort_session_provision(
        self,
        prepared: PreparedSessionProvision[_ResultT],
    ) -> None:
        """Abort one prepared operation exactly once."""
        await prepared.abort_for_owner(self._provision_owner_token)

    async def delete_session(
        self,
        *,
        channel_id: str,
        session_id: str,
        cleanup_session: Callable[..., Awaitable[bool]],
    ) -> SessionDeleteResult:
        """Delete one Session while preserving the established transaction."""
        target = str(session_id or "").strip()
        if not target:
            return SessionDeleteResult.failure(
                target,
                code="BAD_REQUEST",
                message="session_id is required",
            )

        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_history import (
            resolve_session_dir,
        )

        session_dir, invalid_reason = resolve_session_dir(
            target,
            sessions_root=get_agent_sessions_dir(),
        )
        if session_dir is None:
            return SessionDeleteResult.failure(
                target,
                code="BAD_REQUEST",
                message=invalid_reason or "invalid session_id",
            )
        if not session_dir.exists():
            return SessionDeleteResult.failure(
                target,
                code="NOT_FOUND",
                message="session not found",
            )
        if not session_dir.is_dir():
            return SessionDeleteResult.failure(
                target,
                code="BAD_REQUEST",
                message="session is not a directory",
            )

        # Keep one participant for the whole transaction.  AgentServer only
        # replaces this dependency while constructing/rebuilding Runtime, but
        # taking a snapshot also prevents a concurrent host reconfiguration
        # from pairing one lifecycle's begin with another one's abort/commit.
        delete_lifecycle = self._delete_lifecycle
        checkpoint_error = await self._ensure_delete_dependencies(
            target,
            delete_lifecycle=delete_lifecycle,
        )
        if checkpoint_error is not None:
            return checkpoint_error

        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(target)
        is_team_session = is_team_mode(metadata.get("mode"))
        team_name = str(metadata.get("team_name") or "").strip()
        resolved_channel_id = (
            str(metadata.get("channel_id") or channel_id or "").strip() or None
        )
        result = SessionDeleteResult(
            ok=True,
            session_id=target,
            channel_id=resolved_channel_id,
            is_team=is_team_session,
            team_name=team_name,
        )

        self._mark_kvc_session_deleted(result)
        trajectory_prepared = False
        lifecycle_prepared = False
        try:
            if not is_team_session:
                from jiuwenswarm.observability.session_delete import (
                    begin_trajectory_session_delete,
                )

                begin_trajectory_session_delete(target)
                trajectory_prepared = True
            if delete_lifecycle is not None:
                await delete_lifecycle.begin_session_delete(target)
                lifecycle_prepared = True

            if is_team_session:
                deleted = await self._delete_team_session(result)
            else:
                await self._delete_agent_session(
                    result,
                    cleanup_session=cleanup_session,
                )
                deleted = True
            if deleted:
                shutil.rmtree(session_dir)
        except BaseException as exc:
            await self._abort_delete(
                result,
                trajectory_prepared=trajectory_prepared,
                lifecycle_prepared=lifecycle_prepared,
                delete_lifecycle=delete_lifecycle,
            )
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "Runtime session.delete cleanup failed: session_id=%s error=%s",
                target,
                exc,
            )
            return self._cleanup_failed(target)

        if not deleted:
            await self._abort_delete(
                result,
                trajectory_prepared=trajectory_prepared,
                lifecycle_prepared=lifecycle_prepared,
                delete_lifecycle=delete_lifecycle,
            )
            return self._cleanup_failed(target)

        await self._commit_delete_observers(
            result,
            trajectory_prepared=trajectory_prepared,
            lifecycle_prepared=lifecycle_prepared,
            delete_lifecycle=delete_lifecycle,
        )
        return result

    def commit_session_delete(self, result: SessionDeleteResult) -> None:
        """Commit Runtime-owned Plan/cache/binding state after disk deletion."""
        if not result.ok:
            raise ValueError("cannot commit a failed session delete")
        self._plan_controller.reset_session(result.session_id)

        from jiuwenswarm.server.runtime.session.session_metadata import (
            remove_session_metadata_cache,
        )

        remove_session_metadata_cache(result.session_id)
        if not result.is_team:
            return
        try:
            from jiuwenswarm.server.runtime.team_binding_store import (
                get_team_binding_store,
            )

            get_team_binding_store().unbind_session(
                team_name=result.team_name or None,
                session_id=result.session_id,
            )
        except Exception as exc:  # noqa: BLE001 - deletion already committed
            logger.warning(
                "Runtime failed to unbind deleted team session: "
                "session_id=%s team_name=%s error=%s",
                result.session_id,
                result.team_name,
                exc,
            )

    async def _ensure_delete_dependencies(
        self,
        session_id: str,
        *,
        delete_lifecycle: SessionDeleteLifecycle | None,
    ) -> SessionDeleteResult | None:
        try:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                ensure_persistent_checkpointer,
            )

            await ensure_persistent_checkpointer()
        except Exception as exc:  # noqa: BLE001 - public result compatibility
            logger.exception(
                "Runtime persistent checkpointer unavailable: session_id=%s error=%s",
                session_id,
                exc,
            )
            return SessionDeleteResult.failure(
                session_id,
                code="CHECKPOINT_UNAVAILABLE",
                message="persistent checkpointer is unavailable",
            )

        if delete_lifecycle is None or bool(
            getattr(delete_lifecycle, "is_available", True)
        ):
            return None
        start = getattr(delete_lifecycle, "start", None)
        if not callable(start):
            return None
        try:
            await start()
        except Exception as exc:  # noqa: BLE001 - established best effort
            logger.warning(
                "Runtime Session delete lifecycle is not ready yet: %s",
                exc,
            )
        return None

    @staticmethod
    def _cleanup_failed(session_id: str) -> SessionDeleteResult:
        return SessionDeleteResult.failure(
            session_id,
            code="DELETE_FAILED",
            message="session runtime cleanup failed",
        )

    @staticmethod
    def _mark_kvc_session_deleted(result: SessionDeleteResult) -> None:
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                mark_session_deleted,
            )

            mark_session_deleted(
                session_id=result.session_id,
                channel_id=result.channel_id or "default",
                is_team=result.is_team,
            )
        except Exception as exc:  # noqa: BLE001 - established best effort
            logger.warning(
                "Runtime KVC delete tombstone failed; preserving product delete: "
                "session_id=%s error=%s",
                result.session_id,
                exc,
            )

    async def _delete_team_session(self, result: SessionDeleteResult) -> bool:
        from jiuwenswarm.agents.harness.team import get_team_manager

        return await get_team_manager(result.channel_id).delete_session_runtime(
            result.session_id,
            reason="session.delete: ",
        )

    async def _delete_agent_session(
        self,
        result: SessionDeleteResult,
        *,
        cleanup_session: Callable[..., Awaitable[bool]],
    ) -> None:
        await self._agent_manager.release_subagent_runtime_for_session(
            channel_id=result.channel_id,
            session_id=result.session_id,
            reason="session_deleted",
        )
        await cleanup_session(
            channel_id=result.channel_id or "",
            session_id=result.session_id,
            reset_plan_state=False,
        )

        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
            evict_plan_session,
        )

        await evict_plan_session(
            session_id=result.session_id,
        )
        from openjiuwen.core.runner import Runner

        await Runner.release(result.session_id)

    async def _abort_delete(
        self,
        result: SessionDeleteResult,
        *,
        trajectory_prepared: bool,
        lifecycle_prepared: bool,
        delete_lifecycle: SessionDeleteLifecycle | None,
    ) -> None:
        if trajectory_prepared:
            try:
                from jiuwenswarm.observability.session_delete import (
                    abort_trajectory_session_delete,
                )

                abort_trajectory_session_delete(result.session_id)
            except Exception as exc:  # noqa: BLE001 - preserve primary failure
                logger.warning(
                    "Runtime trajectory delete rollback failed: session_id=%s error=%s",
                    result.session_id,
                    exc,
                )
        if lifecycle_prepared and delete_lifecycle is not None:
            try:
                await delete_lifecycle.abort_session_delete(
                    result.session_id,
                    channel_id=result.channel_id or "",
                )
            except Exception as exc:  # noqa: BLE001 - preserve primary failure
                logger.warning(
                    "Runtime Session delete lifecycle rollback failed: "
                    "session_id=%s error=%s",
                    result.session_id,
                    exc,
                )
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                restore_session_after_failed_delete,
            )

            restore_session_after_failed_delete(result.session_id)
        except Exception as exc:  # noqa: BLE001 - preserve primary failure
            logger.warning(
                "Runtime KVC failed-delete rollback failed: session_id=%s error=%s",
                result.session_id,
                exc,
            )

    async def _commit_delete_observers(
        self,
        result: SessionDeleteResult,
        *,
        trajectory_prepared: bool,
        lifecycle_prepared: bool,
        delete_lifecycle: SessionDeleteLifecycle | None,
    ) -> None:
        if trajectory_prepared:
            try:
                from jiuwenswarm.observability.session_delete import (
                    commit_trajectory_session_delete,
                )

                commit_trajectory_session_delete(result.session_id)
            except Exception as exc:  # noqa: BLE001 - deletion already committed
                logger.warning(
                    "Runtime trajectory delete commit failed: session_id=%s error=%s",
                    result.session_id,
                    exc,
                )
        if lifecycle_prepared and delete_lifecycle is not None:
            try:
                await delete_lifecycle.commit_session_delete(
                    result.session_id,
                )
            except Exception as exc:  # noqa: BLE001 - deletion already committed
                logger.warning(
                    "Runtime Session delete lifecycle commit failed: "
                    "session_id=%s error=%s",
                    result.session_id,
                    exc,
                )


__all__ = [
    "PreparedSessionProvision",
    "RuntimeSessionProvisioner",
    "SessionCreateInput",
    "SessionCreateResult",
    "SessionDeleteLifecycle",
    "SessionDeleteResult",
    "SessionDescriptor",
    "SessionForkInput",
    "SessionForkResult",
    "SessionProvisionCommitContext",
    "SessionProvisionCommitTiming",
    "SessionProvisionError",
    "SessionProvisionInput",
    "SessionProvisionResult",
    "SessionProvisionState",
    "SessionProvisionStateError",
    "SessionProvisionerContract",
    "SessionSwitchInput",
    "SessionSwitchResult",
]
