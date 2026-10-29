# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session assembly ownership, singleflight and retryable preparation."""

from __future__ import annotations

import asyncio
import copy
import logging
import inspect
import time
from typing import Any, Callable

from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.config import get_config
from gateway.common.mode_matrix import is_team_mode
from jiuwenswarm.server.runtime.agent_adapter.external_harness_helpers import (
    ExternalHarnessBindingError, load_persisted_external_harness_binding, persist_external_harness_binding,
)


logger = logging.getLogger(__name__)


class SessionAssembler:
    """Own cached children and serialize creation/reload/cleanup per Session.

    The backend supplies permission, rail and SDK construction operations. It
    never publishes execution readiness on its own: ensure() returns only after
    the interaction loop, pending config and history restoration are installed.
    """

    def __init__(
        self, backend: Any, *, history_io: Callable = run_history_io,
        configuration: Callable = get_config,
        load_binding: Callable = load_persisted_external_harness_binding,
        persist_binding: Callable = persist_external_harness_binding,
    ) -> None:
        self._owner = backend
        self._history_io = history_io
        self._configuration = configuration
        self._load_binding = load_binding
        self._persist_binding = persist_binding
        self.adapters: dict[str, Any] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.failed_cleanup: dict[str, Any] = {}
        self._cleanup_errors: dict[str, BaseException] = {}
        self._closing = False

    async def ensure(
        self,
        session_id: str | None,
        *,
        model_name: str | None = None,
        pending_mcp_scan_names: set[str] | None = None,
        history_before_request_id: str | None = None,
        reserve_activity: bool = False,
        host_external_input: bool = False,
        permission_project_dir: str | None = None,
        execution_request: AgentRequest | None = None,
        external_harness_binding: dict[str, Any] | None = None,
        moa_enabled: bool | None = None,
        model_selection: Any = None,
        moa_can_access: Callable[[str, str], bool] | None = None,
        retain_moa_snapshot: bool = False,
    ) -> Any:
        """Return the session-owned adapter, creating and initializing it once."""
        owner = self._owner
        if self._closing:
            raise RuntimeError("session assembler is closing")
        if moa_enabled is not None and not isinstance(moa_enabled, bool):
            raise ValueError("moa_enabled must be a boolean")
        if owner._is_session_scoped_adapter:
            owner._touch_session_adapter(session_id)
            return owner

        sid = owner._session_adapter_key(session_id)
        requested_mcp_scan_names = (
            set(pending_mcp_scan_names) if pending_mcp_scan_names is not None else None
        )
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            if self._closing:
                raise RuntimeError("session assembler is closing")
            failed = self.failed_cleanup.get(sid)
            if failed is not None and not await self._cleanup_failed_child(sid, failed):
                raise RuntimeError(
                    "session_cleanup_pending"
                ) from self._cleanup_errors.get(sid)
            existing = self.adapters.get(sid)
            if external_harness_binding is None and existing is None:
                binding = self._load_binding(sid)
                external_harness_binding = await binding if inspect.isawaitable(binding) else binding

            if existing is not None and external_harness_binding is not None:
                requested_kind = external_harness_binding.get("kind")
                current_binding = getattr(
                    existing,
                    "_external_harness_binding",
                    None,
                )
                is_external_request = requested_kind == "external_harness"
                binding_unchanged = (
                    isinstance(current_binding, dict)
                    and current_binding.get("agent_template_name")
                    == external_harness_binding.get("agent_template_name")
                    and current_binding.get("provider_name")
                    == external_harness_binding.get("provider_name")
                )
                can_rebind_idle_child = (
                    current_binding is None
                    and not existing._session_mcp_reconcile_started  # pylint: disable=protected-access
                    and not existing._is_session_active(sid)  # pylint: disable=protected-access
                )
                if requested_kind == "deep_agent" and current_binding is None:
                    pass
                elif is_external_request and binding_unchanged:
                    pass
                elif is_external_request and can_rebind_idle_child:
                    await existing.cleanup()
                    owner._drop_session_adapter_cache_entry(
                        sid,
                        remove_lock=False,
                    )
                    existing = None
                else:
                    raise ExternalHarnessBindingError(
                        "当前 session child 的运行时与请求不一致；"
                        "请新建会话后切换专家或运行时"
                    )
            effective_moa = False
            is_external_harness = (
                (
                    isinstance(external_harness_binding, dict)
                    and external_harness_binding.get("kind") == "external_harness"
                )
                or getattr(existing, "_external_harness_binding", None) is not None
            )
            if not is_external_harness:
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata, update_session_metadata,
                )

                metadata = get_session_metadata(sid)
                from gateway.common.model_selection import ModelSelection
                from jiuwenswarm.server.runtime.moa_runtime import effective_moa_enabled, resolve_moa_settings

                # Explicit stable selections win; legacy ordinary names are also an
                # explicit switch away from a persisted group. The old boolean is
                # accepted for transport compatibility but never drives execution.
                raw_selection = model_selection
                if raw_selection is None and model_name:
                    if model_name.startswith(("model:", "model_group:")):
                        kind, identifier, *route = model_name.split(":", 2)
                        raw_selection = {"type": kind, "id": identifier, "route_id": route[0] if route else None}
                elif raw_selection is None:
                    raw_selection = metadata.get("model_selection")
                selection = ModelSelection.model_validate(raw_selection) if raw_selection is not None else None
                mode = owner._session_instance_mode
                moa_snapshot = None
                moa_fingerprint = None
                # Same-class children own their execution and permission lifetimes.
                busy = existing is not None and (
                    retain_moa_snapshot
                    or existing._is_session_live(sid)  # pylint: disable=protected-access
                    or existing._has_live_root_permission_owner(sid)  # pylint: disable=protected-access
                )
                if busy:
                    installed_selection = getattr(existing, "_collaboration_model_selection", None)
                    if selection is not None and not getattr(existing, "_moa_enabled", False):
                        suffix = f":{selection.route_id}" if selection.route_id else ""
                        reference = f"{selection.type}:{selection.id}{suffix}"
                        cached = getattr(existing, "_stable_selection_cache", {}).get(reference)
                        if cached is not None and cached[1] is getattr(existing, "_last_resolved_model", None):
                            installed_selection = selection
                    explicit_switch = model_selection is not None or bool(model_name)
                    if not retain_moa_snapshot and explicit_switch and selection != installed_selection:
                        raise RuntimeError("Model selection cannot change while the session is executing")
                    # Global settings and metadata changes never replace a running
                    # or permission-suspended invocation's installed snapshot.
                    effective_moa = getattr(existing, "_moa_enabled", False)
                    selection = installed_selection
                    moa_fingerprint = getattr(existing, "_moa_fingerprint", None)
                    if effective_moa and moa_can_access is not None:
                        # Read the same-class child's installed snapshot, not global settings.
                        resolve_moa_settings(
                            existing._instance_overrides["moa_config_snapshot"],  # pylint: disable=protected-access
                            selection,
                            can_access=moa_can_access,
                        )
                else:
                    moa_snapshot = copy.deepcopy(self._configuration())
                    restoring_saved_selection = model_selection is None and not model_name
                    if selection is not None and (
                        restoring_saved_selection or owner._is_cron_execution_session(sid)
                    ):
                        from jiuwenswarm.common.model_catalog import ModelCatalog
                        from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver

                        selection = ModelSelectionResolver(ModelCatalog(moa_snapshot)).restore(selection)
                    effective_moa = effective_moa_enabled(
                        moa_snapshot, selection, mode=mode,
                        is_cron=owner._is_cron_execution_session(sid),
                    )
                    if effective_moa:
                        _, _, moa_fingerprint = resolve_moa_settings(
                            moa_snapshot, selection,
                            **({"can_access": moa_can_access} if moa_can_access is not None else {}),
                        )
                    else:
                        moa_snapshot = None
                    if model_selection is not None and selection is not None:
                        from jiuwenswarm.server.runtime.session.session_metadata import update_session_model_selection
                        update_session_model_selection(sid, selection.model_dump())
                    elif model_name and selection is None:
                        previous_selection = metadata.get("model_selection")
                        had_group_selection = (
                            isinstance(previous_selection, dict)
                            and previous_selection.get("type") == "model_group"
                        )
                        if (
                            had_group_selection
                            and not is_team_mode(mode)
                            and not owner._is_cron_execution_session(sid)
                        ):
                            # Login/free choices use a legacy name rather than a stable
                            # resource DTO. Clear the old group so it cannot override
                            # that explicit ordinary choice or return after a refresh.
                            from jiuwenswarm.server.runtime.session.session_metadata import (
                                update_session_model_selection,
                            )
                            update_session_model_selection(sid, None)
                            update_session_metadata(
                                session_id=sid, model=model_name, moa_enabled=False,
                                touch_last_message_at=False, sync_write=True,
                            )
                            metadata["moa_enabled"] = False
                if existing is not None and not busy:
                    if (
                        effective_moa != getattr(existing, "_moa_enabled", False)
                        or moa_fingerprint != getattr(existing, "_moa_fingerprint", None)
                    ):
                        # Rebuild under the same lock used for session creation, then
                        # restore disk history before the next turn can enter.
                        await existing.cleanup()
                        owner._drop_session_adapter_cache_entry(sid, remove_lock=False)
                        existing = None
            # Same-class child cleanup remains serialized by this session lock.
            if (
                existing is not None and existing._permission_state.permission_isolated  # pylint: disable=protected-access
            ):
                if not existing._permission_state.permission_cleanup_complete:  # pylint: disable=protected-access
                    await existing._isolate_permission_instance()  # pylint: disable=protected-access
                if not existing._permission_state.permission_cleanup_complete:  # pylint: disable=protected-access
                    raise RuntimeError("permission_session_cleanup_pending")
                owner._drop_session_adapter_cache_entry(
                    sid,
                    remove_lock=False,
                    remove_runtime_state=False,
                )
                existing = None
            if (existing is not None and not is_external_harness and not busy
                    and callable(getattr(existing, "_tool_retrieval_enable_flip_pending", None))
                    and existing._tool_retrieval_enable_flip_pending()):
                await existing.cleanup()
                owner._drop_session_adapter_cache_entry(sid, remove_lock=False)
                existing = None
            if existing is not None:
                first_mcp_reconcile = requested_mcp_scan_names is not None and not bool(
                    getattr(
                        existing,
                        "_session_mcp_reconcile_started",
                        False,
                    )
                )
                frozen_mcp_scan_names = set(
                    getattr(
                        existing,
                        "_initial_skill_scan_mcp_names",
                        frozenset(),
                    )
                    or ()
                )
                should_replace_unused = (
                    first_mcp_reconcile
                    and bool(
                        getattr(
                            existing,
                            "_skill_retrieval_session_enabled",
                            False,
                        )
                    )
                    and (
                        frozen_mcp_scan_names != requested_mcp_scan_names
                        or (
                            not effective_moa
                            and not existing.skill_retrieval_model_profile_matches(model_name)
                        )
                    )
                )
                if should_replace_unused:
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] replacing unused session "
                        "adapter before first MCP reconcile: session_id=%s "
                        "frozen_mcp=%s requested_mcp=%s",
                        sid,
                        sorted(frozen_mcp_scan_names),
                        sorted(requested_mcp_scan_names),
                    )
                    await existing.cleanup()
                    owner._drop_session_adapter_cache_entry(
                        sid,
                        remove_lock=False,
                    )
                    existing = None

            if existing is not None:
                if requested_mcp_scan_names is not None:
                    # Claim the immutable first-chat snapshot while the parent
                    # session lock is still held. Concurrent sends may reconcile
                    # live MCPs later, but cannot dispose the child underneath
                    # this first caller.
                    existing.mark_session_mcp_reconcile_started()
                await owner._reload_session_adapter_if_stale(
                    sid,
                    existing,
                    host_external_input=host_external_input,
                )
                owner._touch_session_adapter(sid)
                if reserve_activity:
                    existing._register_session_agent_task(  # pylint: disable=protected-access
                        sid
                    )
                return existing

            adapter = owner._new_session_scoped_adapter(sid)
            if (
                isinstance(external_harness_binding, dict)
                and external_harness_binding.get("kind") == "external_harness"
            ):
                adapter._external_harness_binding = external_harness_binding  # pylint: disable=protected-access
                binding_write = self._persist_binding(sid, external_harness_binding)
                if inspect.isawaitable(binding_write):
                    await binding_write
                self.adapters[sid] = adapter
                owner._session_adapter_versions[sid] = (
                    owner._session_adapter_config_version
                )
                owner._touch_session_adapter(sid)
                if reserve_activity:
                    adapter._register_session_agent_task(  # pylint: disable=protected-access
                        sid
                    )
                logger.info(
                    "[AgentServer] External Harness session adapter created: "
                    "session_id=%s",
                    sid,
                )
                return adapter
            if execution_request is not None:
                model_name = owner._requested_model_name(execution_request)
            # Execution selection is request-local, rather than a persisted
            # Skill retrieval budget's older model construction choice.
            adapter.configure_initial_execution(
                model_name=model_name,
                request=execution_request,
                can_access=getattr(owner, "_model_selection_can_access", None),
            )
            restored_profile = owner._load_skill_retrieval_session_profile(sid)
            restored_mcp_names = (
                {
                    str(name).strip()
                    for name in restored_profile.get("initial_mcp_names", [])
                    if str(name).strip()
                }
                if restored_profile is not None
                else set(requested_mcp_scan_names or ())
            )
            adapter.restore_skill_retrieval_session(
                restored_profile,
                restored_mcp_names,
                model_name,
            )
            config = (
                dict(owner._session_instance_config)
                if isinstance(owner._session_instance_config, dict)
                else None
            )
            create_started_at = time.monotonic()
            if permission_project_dir is not None:
                config = {**(config or {}), "project_dir": permission_project_dir}
            config = {**(config or {}), "moa_enabled": effective_moa,
                      "moa_model_selection": selection.model_dump() if selection is not None else None}
            if moa_snapshot is not None:
                config["moa_config_snapshot"] = moa_snapshot
                config["moa_can_access"] = moa_can_access
            history_preparation = None
            if history_before_request_id:
                from jiuwenswarm.agents.harness.common.session_ops_service import (
                    prefetch_session_history,
                )

                history_preparation = asyncio.create_task(
                    self._history_io(
                        prefetch_session_history,
                        sid,
                        history_before_request_id=history_before_request_id,
                    ),
                    name=f"session-history-prepare:{sid}",
                )
            try:
                try:
                    await adapter.create_instance(
                        config,
                        mode=owner._session_instance_mode,
                        sub_mode=owner._session_instance_sub_mode,
                        **owner._session_instance_extra_create_kwargs(),
                    )
                    adapter._collaboration_model_selection = selection
                    adapter.persist_skill_retrieval_session_profile()
                    instance_ready_at = time.monotonic()

                    await adapter.start_interaction(session_id=sid)
                    interaction_ready_at = time.monotonic()

                    # A brand-new child uses the template config. Apply any pending
                    # global reload before restoring context or accepting input.
                    await owner._reload_session_adapter_if_stale(
                        sid,
                        adapter,
                        host_external_input=host_external_input,
                        freshly_built=True,
                    )
                    history_snapshot = None
                    if history_preparation is not None:
                        try:
                            history_snapshot = await history_preparation
                        except Exception as exc:
                            logger.debug(
                                "session history preparation failed for %s: %s",
                                sid,
                                exc,
                            )
                    # Restart/eviction restoration still uses the original context
                    # conversion after SDK initialization, start and config reload.
                    try:
                        from jiuwenswarm.agents.harness.common.session_ops_service import (
                            warmup_session_context,
                        )

                        await warmup_session_context(
                            deep_agent=getattr(adapter, "_instance", None),
                            session_id=sid,
                            history_before_request_id=history_before_request_id,
                            **(
                                {"history_snapshot": history_snapshot}
                                if history_snapshot is not None
                                else {}
                            ),
                        )
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] session context warmup failed: "
                            "session_id=%s error=%s",
                            sid,
                            exc,
                        )
                finally:
                    await owner._drain_history_preparation(history_preparation)
            except BaseException:
                await self._cleanup_failed_child(sid, adapter)
                raise
            if metadata.get("moa_enabled") is not effective_moa:
                update_session_metadata(
                    session_id=sid, moa_enabled=effective_moa,
                    touch_last_message_at=False, sync_write=True,
                )
            # Cache readers outside ensure() must never observe a partially
            # installed permission/interaction/history owner.
            self.adapters[sid] = adapter
            if requested_mcp_scan_names is not None or restored_profile is not None:
                adapter.mark_session_mcp_reconcile_started()
            owner._touch_session_adapter(sid)
            # Cold-start cost of a session's first turn, split so a slow one can
            # be attributed to agent assembly vs. interaction startup.
            logger.info(
                "[AgentServer] session adapter created: session_id=%s create_instance_ms=%.1f"
                " start_interaction_ms=%.1f total_ms=%.1f",
                sid,
                (instance_ready_at - create_started_at) * 1000,
                (interaction_ready_at - instance_ready_at) * 1000,
                (time.monotonic() - create_started_at) * 1000,
            )
            if reserve_activity:
                adapter._register_session_agent_task(  # pylint: disable=protected-access
                    sid
                )
            return adapter

    async def _cleanup_failed_child(self, session_id: str, adapter: Any) -> bool:
        """Keep the singleflight lock until failed resources have drained."""
        if self.adapters.get(session_id) is adapter:
            self.adapters.pop(session_id, None)
        # A failed cleanup remains a resource owner, never an executable child.
        # Both the next ensure() and parent shutdown must retry this exact owner
        # before another child can be installed for the Session.
        self.failed_cleanup[session_id] = adapter
        for name in (
            "_session_adapter_last_used",
            "_session_adapter_versions",
            "_session_adapter_reload_failures",
        ):
            values = getattr(self._owner, name, None)
            if isinstance(values, dict):
                values.pop(session_id, None)
        cleanup = getattr(adapter, "cleanup", None)
        if not callable(cleanup):
            self.failed_cleanup.pop(session_id, None)
            self._cleanup_errors.pop(session_id, None)
            return True
        task = asyncio.ensure_future(cleanup())
        cancelled = None
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    self._cleanup_errors[session_id] = exc
                    logger.warning("failed child cleanup was cancelled: %s", session_id)
                    return False
                # Repeated shutdown/disconnect cancellation cannot publish the
                # next child while the old owner still holds SDK resources.
                cancelled = exc
            except Exception as exc:
                self._cleanup_errors[session_id] = exc
                logger.exception("failed session assembly cleanup: %s", session_id)
                return False
            else:
                self.failed_cleanup.pop(session_id, None)
                self._cleanup_errors.pop(session_id, None)
                if cancelled is not None:
                    raise cancelled
                return True

    async def cleanup_children(self) -> None:
        """Drain cached and failed owners, retaining every unfinished cleanup."""
        self._closing = True
        # Include in-flight assembly locks: one may publish its completed child
        # while shutdown waits to acquire that Session's lock.
        sessions = dict.fromkeys((*self.adapters, *self.failed_cleanup, *self.locks))
        incomplete = []
        for sid in sessions:
            async with self.locks.setdefault(sid, asyncio.Lock()):
                adapter = self.failed_cleanup.get(sid, self.adapters.get(sid))
                if adapter is None:
                    continue
                if not await self._cleanup_failed_child(sid, adapter):
                    incomplete.append(sid)
        if incomplete:
            sid = incomplete[0]
            raise RuntimeError("session_cleanup_pending") from self._cleanup_errors.get(
                sid
            )
