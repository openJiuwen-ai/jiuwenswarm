# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral product Session lifecycle contracts and orchestration.

The provisioner owns the business transaction behind ``session.delete`` and
defines the staged contract used to move create, switch, and fork behind the
same Runtime boundary.  AgentServer remains responsible for connection locks,
view identity, request/wire translation, and response delivery.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import AsyncExitStack
import logging
import os
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Generic, Protocol, TypeAlias, TypeVar
from weakref import WeakValueDictionary

from jiuwenswarm.runtime.session_delete import (
    TEAM_DELETION_GATE,
    SessionDeleteResult,
    TeamDeleteResult,
    session_delete_lock,
    team_delete_lock,
)
from jiuwenswarm.runtime.session_lifecycle import (
    RuntimeParticipantRegistry,
    SessionDescriptor,
    SessionForegroundEvent,
    SessionKind,
    SessionLifecycleTarget,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jiuwenswarm.runtime.plan import PlanModeController
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

logger = logging.getLogger(__name__)

_LEGACY_WORK_MODE_MISMATCH_SEPARATOR = " " * 37

# Preserve the established same-process serialization for explicit TUI IDs,
# even when more than one Runtime instance exists. Weak values avoid retaining
# one lock for every historical Session after no operation references it.
_EXTERNAL_CREATE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_DELETE_RESULT_CACHE_MAX_ENTRIES = 256
_DELETE_RESULT_CACHE_TTL_SECONDS = 60.0

_DeleteResultT = TypeVar("_DeleteResultT")


class _RecentDeleteResults(Generic[_DeleteResultT]):
    """Bounded retry cache; it is not a durable record of deleted identities."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[float, _DeleteResultT]] = OrderedDict()

    def get(self, key: str) -> _DeleteResultT | None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        expires_at, result = entry
        if expires_at <= time.monotonic():
            return None
        self._entries[key] = entry
        return result

    def __setitem__(self, key: str, result: _DeleteResultT) -> None:
        self._entries.pop(key, None)
        self._entries[key] = (
            time.monotonic() + _DELETE_RESULT_CACHE_TTL_SECONDS,
            result,
        )
        while len(self._entries) > _DELETE_RESULT_CACHE_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def pop(self, key: str, default: object = None) -> _DeleteResultT | object:
        entry = self._entries.pop(key, None)
        return entry[1] if entry is not None else default

    def __len__(self) -> int:
        return len(self._entries)


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
    cutoff_message_id: str = ""
    cutoff_role: str = ""
    cutoff_content: str = ""
    cutoff_timestamp: float | str | None = None
    session_equipment_override: dict[str, object] | None = None


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


class RuntimeSessionProvisioner:
    """Coordinate transport-neutral Session lifecycle work for one Runtime."""

    def __init__(
        self,
        *,
        agent_manager: AgentManager,
        plan_controller: PlanModeController,
        delete_lifecycle: SessionDeleteLifecycle | None = None,
        participant_registry: RuntimeParticipantRegistry | None = None,
        team_execution_controller: object | None = None,
    ) -> None:
        self._agent_manager = agent_manager
        self._plan_controller = plan_controller
        self._delete_lifecycle = delete_lifecycle
        self._participant_registry = (
            participant_registry or RuntimeParticipantRegistry()
        )
        self._team_execution_controller = team_execution_controller
        self._provision_owner_token = object()
        self._external_create_locks = _EXTERNAL_CREATE_LOCKS
        self._background_create_kvc_tasks: set[asyncio.Task[None]] = set()
        self._completed_session_deletes = _RecentDeleteResults[SessionDeleteResult]()
        self._completed_team_deletes = _RecentDeleteResults[TeamDeleteResult]()

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
        switch_context: SessionLifecycleTarget | None = None,
        dispatch_signals: SessionLifecycleTarget | None = None,
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
                self._completed_session_deletes.pop(result.session_id, None)
                if switch_context is not None:
                    task = asyncio.create_task(
                        self._dispatch_foreground_transition(
                            target=switch_context,
                            previous=dispatch_signals,
                            view_id=context.foreground_scope_id or "default-view",
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
            self._background_create_kvc_tasks.difference_update(tasks)

    async def _prepare_create_owner(
        self,
        *,
        channel_id: str,
        session_id: str,
        previous_session_id: str,
        params: dict[str, object],
    ) -> tuple[SessionLifecycleTarget, SessionLifecycleTarget | None]:
        from jiuwenswarm.common.mode_matrix import is_team_mode

        mode = str(params.get("mode") or "agent.plan")
        target_is_team = bool(params.get("team")) or is_team_mode(mode)
        target = SessionLifecycleTarget(
            descriptor=SessionDescriptor(
                session_id=session_id,
                channel_id=channel_id,
                mode=mode,
                work_mode=str(params.get("work_mode") or "work"),
                project_id=str(params.get("project_id") or ""),
                project_dir=str(params.get("project_dir") or params.get("cwd") or ""),
                user_id=str(params.get("user_id") or ""),
            ),
            kind=SessionKind.TEAM if target_is_team else SessionKind.AGENT,
            team_name=str(params.get("team_name") or ""),
        )
        previous_target = self._participant_registry.target(previous_session_id)
        previous_is_team = bool(
            previous_target and previous_target.kind is SessionKind.TEAM
        )
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            await get_team_manager(channel_id).prepare_session_switch(
                session_id,
                previous_session_id=(previous_session_id if previous_is_team else None),
                reason="session.create switch: ",
            )
        return target, previous_target

    async def _dispatch_foreground_transition(
        self,
        *,
        target: SessionLifecycleTarget,
        previous: SessionLifecycleTarget | None,
        view_id: str,
    ) -> None:
        participants = self._participant_registry.snapshot_activity()
        if not participants:
            self._participant_registry.remember_target(target)
            return
        from jiuwenswarm.server.runtime.session.session_history import history_exists

        if (
            previous is not None
            and previous.descriptor.session_id != target.descriptor.session_id
        ):
            previous_event = SessionForegroundEvent(
                target=previous,
                view_id=view_id,
                visible=False,
                has_history=history_exists(previous.descriptor.session_id),
            )
            for participant in participants:
                try:
                    await participant.foreground_changed(previous_event)
                except Exception as exc:
                    logger.warning("Runtime foreground participant failed: %s", exc)
        self._participant_registry.remember_target(target)
        target_event = SessionForegroundEvent(
            target=target,
            view_id=view_id,
            visible=True,
            has_history=history_exists(target.descriptor.session_id),
        )
        for participant in participants:
            try:
                await participant.foreground_changed(target_event)
            except Exception as exc:
                logger.warning("Runtime foreground participant failed: %s", exc)

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
        target_session_id = str(provision_input.target_session_id or "").strip()
        previous_session_id = str(provision_input.previous_session_id or "").strip()
        if not target_session_id:
            raise SessionProvisionError(
                "session_id is required",
                code="BAD_REQUEST",
            )

        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.runtime.request import resolve_agent_request_mode

        _, _, resolved_mode = resolve_agent_request_mode(provision_input.mode)
        target_is_team = provision_input.team_hint or is_team_mode(provision_input.mode)
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        target_metadata = get_session_metadata(target_session_id)
        stored_mode = str(target_metadata.get("mode") or "").strip()
        if stored_mode:
            _, _, resolved_mode = resolve_agent_request_mode(stored_mode)
            target_is_team = is_team_mode(stored_mode)
        target = SessionLifecycleTarget(
            descriptor=SessionDescriptor(
                session_id=target_session_id,
                channel_id=channel_id,
                mode=resolved_mode,
                work_mode="work",
            ),
            kind=SessionKind.TEAM if target_is_team else SessionKind.AGENT,
        )
        previous = self._participant_registry.target(previous_session_id)
        if previous is None and previous_session_id:
            previous_metadata = get_session_metadata(previous_session_id)
            previous_mode = str(
                previous_metadata.get("mode") or provision_input.previous_mode or ""
            )
            if previous_mode:
                previous = SessionLifecycleTarget(
                    descriptor=SessionDescriptor(
                        session_id=previous_session_id,
                        channel_id=str(
                            previous_metadata.get("channel_id") or channel_id
                        ),
                        mode=previous_mode,
                        work_mode=str(previous_metadata.get("work_mode") or "work"),
                    ),
                    kind=(
                        SessionKind.TEAM
                        if is_team_mode(previous_mode)
                        else SessionKind.AGENT
                    ),
                    team_name=str(previous_metadata.get("team_name") or ""),
                )
        previous_is_team = bool(previous and previous.kind is SessionKind.TEAM)
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_manager = get_team_manager(channel_id)
            await team_manager.prepare_session_switch(
                target_session_id,
                previous_session_id=(previous_session_id if previous_is_team else None),
                reason="session.switch: ",
            )

        async def commit_switch(
            context: SessionProvisionCommitContext,
        ) -> None:
            await self._dispatch_foreground_transition(
                target=target,
                previous=previous,
                view_id=context.foreground_scope_id or "default-view",
            )

        result = SessionSwitchResult(
            channel_id=channel_id,
            session_id=target_session_id,
            mode=resolved_mode,
        )
        return self._stage_session_provision(
            result,
            commit_timing=(SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY),
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
        cutoff_message_id = str(
            provision_input.cutoff_message_id or ""
        ).strip()
        cutoff_role = str(provision_input.cutoff_role or "").strip()
        cutoff_content = str(provision_input.cutoff_content or "")
        cutoff_timestamp = provision_input.cutoff_timestamp
        has_message_cutoff = bool(
            cutoff_message_id
            or cutoff_role
            or cutoff_content
            or cutoff_timestamp is not None
        )

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

            fork_kwargs: dict[str, object] = {
                "source_session_id": source_session_id,
                "target_session_id": target_session_id,
                "title": title,
                "channel_id": channel_id,
            }
            if has_message_cutoff:
                fork_kwargs.update(
                    {
                        "cutoff_message_id": cutoff_message_id,
                        "cutoff_role": cutoff_role,
                        "cutoff_content": cutoff_content,
                        "cutoff_timestamp": cutoff_timestamp,
                    }
                )
            if provision_input.session_equipment_override is not None:
                fork_kwargs["session_equipment_override"] = provision_input.session_equipment_override
            fork_result = fork_session(
                **fork_kwargs,
            )

            agent = self._agent_manager.get_agent_nowait(channel_id)
            deep_agent = None
            if agent is not None:
                deep_agent = await agent.ensure_instance()
                if has_message_cutoff:
                    await copy_session_context(
                        deep_agent,
                        source_session_id,
                        target_session_id,
                        force_history=True,
                    )
                else:
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

            if not has_message_cutoff:
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
        quiesce_session: Callable[..., Awaitable[None]],
        dispose_session: Callable[..., Awaitable[None]],
    ) -> SessionDeleteResult:
        target = str(session_id or "").strip()
        if not target:
            return SessionDeleteResult.failure(
                target,
                code="BAD_REQUEST",
                message="session_id is required",
            )
        async with session_delete_lock(target):
            return await self._delete_session_locked(
                channel_id=channel_id,
                session_id=target,
                quiesce_session=quiesce_session,
                dispose_session=dispose_session,
            )

    async def _delete_session_locked(
        self,
        *,
        channel_id: str,
        session_id: str,
        quiesce_session: Callable[..., Awaitable[None]],
        dispose_session: Callable[..., Awaitable[None]],
    ) -> SessionDeleteResult:
        """Delete one Session while preserving the established transaction."""
        target = str(session_id or "").strip()

        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_history import (
            resolve_session_dir,
        )

        from jiuwenswarm.server.runtime.session.lifecycle import (
            session_paths,
            LifecycleError,
            state as lifecycle_state,
            update as lifecycle_update,
        )

        try:
            session_dir, invalid_reason = resolve_session_dir(
                target, sessions_root=get_agent_sessions_dir()
            )
            if session_dir is not None:
                _, archived_dir = session_paths(target)
                if archived_dir.exists():
                    session_dir = archived_dir
        except LifecycleError as exc:
            return SessionDeleteResult.failure(target, code=exc.code, message=str(exc))
        if session_dir is None:
            return SessionDeleteResult.failure(
                target,
                code="BAD_REQUEST",
                message=invalid_reason or "invalid session_id",
            )
        delete_operation = lifecycle_state("session", target).get("operation", {})
        recovering_delete = (
            delete_operation.get("kind") == "delete"
            and delete_operation.get("status") != "completed"
        )
        if not session_dir.exists() and not recovering_delete:
            completed = self._completed_session_deletes.get(target)
            if completed is not None:
                return completed
            return SessionDeleteResult.failure(
                target,
                code="NOT_FOUND",
                message="session not found",
            )
        if session_dir.exists() and not session_dir.is_dir():
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

        metadata = get_session_metadata(target) or delete_operation.get(
            "delete_metadata", {}
        )
        if recovering_delete and not delete_operation.get("delete_metadata"):
            lifecycle_update("session", target, delete_metadata=metadata)
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
        descriptor = SessionDescriptor(
            session_id=target,
            channel_id=resolved_channel_id or "default",
            mode=str(metadata.get("mode") or "agent.plan"),
            work_mode=str(metadata.get("work_mode") or "work"),
            project_id=str(metadata.get("project_id") or ""),
            project_dir=str(metadata.get("project_dir") or ""),
            user_id=str(metadata.get("user_id") or ""),
        )
        delete_target = SessionLifecycleTarget(
            descriptor=descriptor,
            kind=SessionKind.TEAM if is_team_session else SessionKind.AGENT,
            team_name=team_name,
        )
        team_controller = self._team_execution_controller if is_team_session else None
        team_delete_quiesced = False
        participants = self._participant_registry.snapshot_delete()
        entered_participants = []
        for participant in participants:
            entered_participants.append(participant)
            try:
                await participant.before_delete(delete_target)
            except asyncio.CancelledError:
                await self._notify_delete_failed(
                    tuple(entered_participants),
                    delete_target,
                    destructive_started=False,
                )
                raise
            except Exception as exc:
                logger.warning(
                    "Runtime delete participant before_delete failed: "
                    "participant=%s session_id=%s error=%s",
                    participant.name,
                    target,
                    exc,
                )

        trajectory_prepared = False
        lifecycle_prepared = False
        destructive_started = False
        try:
            from jiuwenswarm.observability.session_delete import (
                begin_trajectory_session_delete,
            )

            # Team and single-agent sessions both own a trajectory database.
            # Draining the ingress joins that session's writer threads, so it
            # must not run on the event loop.
            await asyncio.to_thread(begin_trajectory_session_delete, target)
            trajectory_prepared = True
            if delete_lifecycle is not None:
                await delete_lifecycle.begin_session_delete(target)
                lifecycle_prepared = True

            if not session_dir.exists() and recovering_delete:
                deleted = True
            elif is_team_session:
                if team_controller is None:
                    raise RuntimeError("team execution controller is unavailable")
                await team_controller.quiesce_for_delete(
                    delete_target,
                    reason="session.delete: ",
                )
                team_delete_quiesced = True
                await self._release_participants(
                    tuple(entered_participants),
                    delete_target,
                )
                destructive_started = True
                await team_controller.dispose_after_resource_release(
                    delete_target,
                    reason="session.delete: ",
                )
                deleted = await self._delete_team_runner(result)
            else:
                await quiesce_session(
                    channel_id=result.channel_id or "",
                    session_id=result.session_id,
                )
                await self._release_participants(
                    tuple(entered_participants),
                    delete_target,
                )
                destructive_started = True
                await dispose_session(
                    channel_id=result.channel_id or "",
                    session_id=result.session_id,
                )
                from openjiuwen.core.runner import Runner

                await Runner.release(result.session_id)
                deleted = True
            if deleted and session_dir.exists():
                shutil.rmtree(session_dir)
            if deleted and recovering_delete:
                lifecycle_update("session", target, phase="cleanup")
        except BaseException as exc:
            if (
                team_controller is not None
                and team_delete_quiesced
                and not destructive_started
            ):
                team_controller.delete_aborted(delete_target)
            await self._abort_delete(
                result,
                trajectory_prepared=trajectory_prepared,
                lifecycle_prepared=lifecycle_prepared,
                delete_lifecycle=delete_lifecycle,
                destructive_started=destructive_started,
            )
            await self._notify_delete_failed(
                tuple(entered_participants),
                delete_target,
                destructive_started=destructive_started,
            )
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "Runtime session.delete cleanup failed: session_id=%s error=%s",
                target,
                exc,
            )
            return self._cleanup_failed(
                result,
                destructive_started=destructive_started,
            )

        if not deleted:
            if (
                team_controller is not None
                and team_delete_quiesced
                and not destructive_started
            ):
                team_controller.delete_aborted(delete_target)
            await self._abort_delete(
                result,
                trajectory_prepared=trajectory_prepared,
                lifecycle_prepared=lifecycle_prepared,
                delete_lifecycle=delete_lifecycle,
                destructive_started=destructive_started,
            )
            await self._notify_delete_failed(
                tuple(entered_participants),
                delete_target,
                destructive_started=destructive_started,
            )
            return self._cleanup_failed(
                result,
                destructive_started=destructive_started,
            )

        try:
            await self._commit_delete_observers(
                result,
                trajectory_prepared=trajectory_prepared,
                lifecycle_prepared=lifecycle_prepared,
                delete_lifecycle=delete_lifecycle,
            )
            committed_result = SessionDeleteResult(
                ok=True,
                session_id=result.session_id,
                channel_id=result.channel_id,
                is_team=result.is_team,
                team_name=result.team_name,
                deleted=True,
            )
            self.commit_session_delete(committed_result)
            if team_controller is not None:
                team_controller.delete_committed(delete_target)
        except asyncio.CancelledError:
            await self._notify_delete_failed(
                tuple(entered_participants),
                delete_target,
                destructive_started=True,
            )
            raise
        except Exception as exc:
            logger.warning(
                "Runtime Session delete commit remains pending: session_id=%s error=%s",
                target,
                exc,
            )
            await self._notify_delete_failed(
                tuple(entered_participants),
                delete_target,
                destructive_started=True,
            )
            return SessionDeleteResult.failure(
                target,
                code="DELETE_COMMIT_PENDING",
                message="session was deleted but commit is pending",
                deleted=True,
                recovery_required=True,
                channel_id=result.channel_id,
                is_team=result.is_team,
                team_name=result.team_name,
            )
        await self._notify_delete_committed(
            tuple(entered_participants),
            delete_target,
        )
        self._completed_session_deletes[target] = committed_result
        return committed_result

    async def delete_team(
        self,
        *,
        team_name: str,
        channel_id: str = "",
    ) -> TeamDeleteResult:
        """Delete one Team and all of its persisted Sessions as one operation."""
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
            get_team_binding_store,
            validate_team_name,
        )
        from jiuwenswarm.server.runtime.team_entity_store import (
            TeamEntityStoreError,
            get_team_entity_store,
        )

        try:
            normalized = validate_team_name(team_name)
        except TeamBindingStoreError as exc:
            return TeamDeleteResult(
                ok=False,
                team_name=str(team_name or "").strip(),
                error_code=exc.code,
                error_message=str(exc),
            )

        async with team_delete_lock(normalized):
            gate_lock = TEAM_DELETION_GATE.mutation_lock(normalized)
            async with gate_lock:
                created_marker = TEAM_DELETION_GATE.begin_delete_locked(normalized)
                binding_store = get_team_binding_store()
                entity_store = get_team_entity_store()
                binding = binding_store.get(normalized)
                session_ids = self._inventory_team_session_ids(
                    normalized,
                    binding_session_ids=(binding.session_ids if binding else ()),
                )

            if not session_ids:
                if binding is None and not entity_store.exists(normalized):
                    completed = self._completed_team_deletes.get(normalized)
                    if created_marker:
                        async with gate_lock:
                            TEAM_DELETION_GATE.finish_delete_locked(normalized)
                    if completed is not None:
                        return completed
                    return TeamDeleteResult(
                        ok=False,
                        team_name=normalized,
                        error_code="NOT_FOUND",
                        error_message="team not found",
                    )
                try:
                    entity_store.delete_team_directory(normalized)
                    binding_store.delete(normalized)
                except (TeamBindingStoreError, TeamEntityStoreError) as exc:
                    if created_marker:
                        async with gate_lock:
                            TEAM_DELETION_GATE.finish_delete_locked(normalized)
                    return TeamDeleteResult(
                        ok=False,
                        team_name=normalized,
                        error_code=getattr(exc, "code", "DELETE_FAILED"),
                        error_message=str(exc),
                    )
                async with gate_lock:
                    TEAM_DELETION_GATE.finish_delete_locked(normalized)
                result = TeamDeleteResult(
                    ok=True,
                    team_name=normalized,
                    deleted=True,
                )
                self._completed_team_deletes[normalized] = result
                return result

            dependency_error = await self._ensure_delete_dependencies(
                session_ids[0],
                delete_lifecycle=self._delete_lifecycle,
            )
            if dependency_error is not None:
                if created_marker:
                    async with gate_lock:
                        TEAM_DELETION_GATE.finish_delete_locked(normalized)
                return TeamDeleteResult(
                    ok=False,
                    team_name=normalized,
                    failed_session_ids=tuple(session_ids),
                    error_code=dependency_error.error_code,
                    error_message=dependency_error.error_message,
                )

            async with AsyncExitStack() as stack:
                for session_id in session_ids:
                    await stack.enter_async_context(session_delete_lock(session_id))
                return await self._delete_team_locked(
                    team_name=normalized,
                    channel_id=channel_id,
                    session_ids=session_ids,
                    binding_store=binding_store,
                    entity_store=entity_store,
                    gate_lock=gate_lock,
                    created_marker=created_marker,
                )

    @staticmethod
    def _inventory_team_session_ids(
        team_name: str,
        *,
        binding_session_ids: tuple[str, ...],
    ) -> list[str]:
        from jiuwenswarm.common.mode_matrix import is_team_mode
        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        matched = {
            str(item).strip() for item in binding_session_ids if str(item).strip()
        }
        active_root = get_agent_sessions_dir()
        for root in (active_root, active_root.parent / "sessions_archived"):
            if not root.exists():
                continue
            for path in root.iterdir():
                if not path.is_dir():
                    continue
                metadata = get_session_metadata(path.name)
                if (
                    is_team_mode(metadata.get("mode"))
                    and str(metadata.get("team_name") or "").strip() == team_name
                ):
                    matched.add(path.name)
        return sorted(matched)

    async def _delete_team_locked(
        self,
        *,
        team_name: str,
        channel_id: str,
        session_ids: list[str],
        binding_store: object,
        entity_store: object,
        gate_lock: asyncio.Lock,
        created_marker: bool,
    ) -> TeamDeleteResult:
        from jiuwenswarm.server.runtime.session.lifecycle import session_paths
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        controller = self._team_execution_controller
        if controller is None:
            if created_marker:
                async with gate_lock:
                    TEAM_DELETION_GATE.finish_delete_locked(team_name)
            return TeamDeleteResult(
                ok=False,
                team_name=team_name,
                failed_session_ids=tuple(session_ids),
                error_code="DELETE_FAILED",
                error_message="team execution controller is unavailable",
            )

        participants = self._participant_registry.snapshot_delete()
        operations: list[
            tuple[SessionDeleteResult, SessionLifecycleTarget, tuple[object, ...]]
        ] = []
        lifecycle_prepared: set[str] = set()
        destructive_started = False
        try:
            for session_id in session_ids:
                metadata = get_session_metadata(session_id)
                resolved_channel = (
                    str(metadata.get("channel_id") or channel_id or "").strip() or None
                )
                result = SessionDeleteResult(
                    ok=True,
                    session_id=session_id,
                    channel_id=resolved_channel,
                    is_team=True,
                    team_name=team_name,
                )
                target = SessionLifecycleTarget(
                    descriptor=SessionDescriptor(
                        session_id=session_id,
                        channel_id=resolved_channel or "default",
                        mode=str(metadata.get("mode") or "team"),
                        work_mode=str(metadata.get("work_mode") or "work"),
                        project_id=str(metadata.get("project_id") or ""),
                        project_dir=str(metadata.get("project_dir") or ""),
                        user_id=str(metadata.get("user_id") or ""),
                    ),
                    kind=SessionKind.TEAM,
                    team_name=team_name,
                )
                entered: list[object] = []
                for participant in participants:
                    entered.append(participant)
                    try:
                        await participant.before_delete(target)
                    except asyncio.CancelledError:
                        await self._notify_delete_failed(
                            tuple(entered),
                            target,
                            destructive_started=False,
                        )
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Runtime Team delete participant before failed: "
                            "participant=%s session_id=%s error=%s",
                            participant.name,
                            session_id,
                            exc,
                        )
                operations.append((result, target, tuple(entered)))

            for result, target, _entered in operations:
                if self._delete_lifecycle is not None:
                    await self._delete_lifecycle.begin_session_delete(result.session_id)
                    lifecycle_prepared.add(result.session_id)
                await controller.quiesce_for_delete(target, reason="team.delete: ")

            for _result, target, entered in operations:
                await self._release_participants(entered, target)

            destructive_started = True
            for _result, target, _entered in operations:
                await controller.dispose_after_resource_release(
                    target,
                    reason="team.delete: ",
                )

            from openjiuwen.core.runner import Runner

            runner_deleted = await Runner.delete_agent_team(
                team_name=team_name,
                session_ids=session_ids,
                force=True,
            )
            if runner_deleted is False:
                raise RuntimeError("agent team runtime cleanup failed")
        except BaseException as exc:
            for result, target, entered in reversed(operations):
                if not destructive_started and result.session_id in lifecycle_prepared:
                    await self._abort_delete(
                        result,
                        trajectory_prepared=False,
                        lifecycle_prepared=True,
                        delete_lifecycle=self._delete_lifecycle,
                        destructive_started=False,
                    )
                await self._notify_delete_failed(
                    entered,
                    target,
                    destructive_started=destructive_started,
                )
                if not destructive_started:
                    controller.delete_aborted(target)
            if not destructive_started and created_marker:
                async with gate_lock:
                    TEAM_DELETION_GATE.finish_delete_locked(team_name)
            if not isinstance(exc, Exception):
                raise
            return TeamDeleteResult(
                ok=False,
                team_name=team_name,
                failed_session_ids=tuple(session_ids),
                recovery_required=destructive_started,
                error_code="DELETE_FAILED",
                error_message=str(exc),
            )

        succeeded: list[str] = []
        failed: list[str] = []
        for result, target, entered in operations:
            active_path, archived_path = session_paths(result.session_id)
            session_dir = archived_path if archived_path.exists() else active_path
            try:
                if session_dir.exists():
                    shutil.rmtree(session_dir)
                if self._delete_lifecycle is not None:
                    await self._delete_lifecycle.commit_session_delete(
                        result.session_id
                    )
                committed = SessionDeleteResult(
                    ok=True,
                    session_id=result.session_id,
                    channel_id=result.channel_id,
                    is_team=True,
                    team_name=team_name,
                    deleted=True,
                )
                self.commit_session_delete(committed)
                controller.delete_committed(target)
                await self._notify_delete_committed(entered, target)
                succeeded.append(result.session_id)
            except Exception as exc:
                logger.warning(
                    "Runtime Team Session storage/commit failed: "
                    "session_id=%s error=%s",
                    result.session_id,
                    exc,
                )
                await self._notify_delete_failed(
                    entered,
                    target,
                    destructive_started=True,
                )
                failed.append(result.session_id)

        if failed:
            return TeamDeleteResult(
                ok=False,
                team_name=team_name,
                session_ids=tuple(succeeded),
                failed_session_ids=tuple(failed),
                recovery_required=True,
                error_code="DELETE_FAILED",
                error_message="failed to delete one or more Team Sessions",
            )

        try:
            entity_store.delete_team_directory(team_name)
            binding_store.delete(team_name)
        except Exception as exc:
            return TeamDeleteResult(
                ok=False,
                team_name=team_name,
                session_ids=tuple(succeeded),
                recovery_required=True,
                error_code=getattr(exc, "code", "DELETE_FAILED"),
                error_message=str(exc),
            )
        async with gate_lock:
            TEAM_DELETION_GATE.finish_delete_locked(team_name)
        result = TeamDeleteResult(
            ok=True,
            team_name=team_name,
            session_ids=tuple(succeeded),
            deleted=True,
        )
        self._completed_team_deletes[team_name] = result
        return result

    def commit_session_delete(self, result: SessionDeleteResult) -> None:
        """Commit Runtime-owned Plan/cache/binding state after disk deletion."""
        if not result.ok:
            raise ValueError("cannot commit a failed session delete")
        self._plan_controller.reset_session(result.session_id)
        self._participant_registry.forget_target(result.session_id)

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
            raise

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
    def _cleanup_failed(
        result: SessionDeleteResult,
        *,
        destructive_started: bool,
    ) -> SessionDeleteResult:
        return SessionDeleteResult.failure(
            result.session_id,
            code="DELETE_FAILED",
            message="session runtime cleanup failed",
            deleted=False,
            recovery_required=destructive_started,
            channel_id=result.channel_id,
            is_team=result.is_team,
            team_name=result.team_name,
        )

    @staticmethod
    async def _delete_team_runner(result: SessionDeleteResult) -> bool:
        from openjiuwen.core.runner import Runner

        if result.team_name:
            deleted = await Runner.delete_agent_team(
                team_name=result.team_name,
                session_ids=[result.session_id],
                force=True,
            )
            return deleted is not False
        logger.warning(
            "Runtime team Session delete fell back to Runner.release: "
            "session_id=%s reason=missing_team_name",
            result.session_id,
        )
        await Runner.release(result.session_id)
        return True

    async def _abort_delete(
        self,
        result: SessionDeleteResult,
        *,
        trajectory_prepared: bool,
        lifecycle_prepared: bool,
        delete_lifecycle: SessionDeleteLifecycle | None,
        destructive_started: bool,
    ) -> None:
        if destructive_started:
            return
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

    @staticmethod
    async def _release_participants(
        participants: tuple[object, ...],
        target: SessionLifecycleTarget,
    ) -> None:
        for participant in participants:
            try:
                await participant.release_resources(target)
            except Exception as exc:
                logger.warning(
                    "Runtime delete participant release failed: "
                    "participant=%s session_id=%s error=%s",
                    participant.name,
                    target.descriptor.session_id,
                    exc,
                )

    @staticmethod
    async def _notify_delete_failed(
        participants: tuple[object, ...],
        target: SessionLifecycleTarget,
        *,
        destructive_started: bool,
    ) -> None:
        for participant in reversed(participants):
            try:
                await participant.delete_failed(
                    target,
                    destructive_started=destructive_started,
                )
            except Exception as exc:
                logger.warning(
                    "Runtime delete participant failure notification failed: "
                    "participant=%s session_id=%s error=%s",
                    participant.name,
                    target.descriptor.session_id,
                    exc,
                )

    @staticmethod
    async def _notify_delete_committed(
        participants: tuple[object, ...],
        target: SessionLifecycleTarget,
    ) -> None:
        for participant in participants:
            try:
                await participant.delete_committed(target)
            except Exception as exc:
                logger.warning(
                    "Runtime delete participant commit notification failed: "
                    "participant=%s session_id=%s error=%s",
                    participant.name,
                    target.descriptor.session_id,
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

                # Joins the session's route threads again before unlinking its
                # database files; keep it off the event loop.
                await asyncio.to_thread(
                    commit_trajectory_session_delete,
                    result.session_id,
                )
            except Exception as exc:  # noqa: BLE001 - deletion already committed
                logger.warning(
                    "Runtime trajectory delete commit failed: session_id=%s error=%s",
                    result.session_id,
                    exc,
                )
                raise
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
                raise


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
