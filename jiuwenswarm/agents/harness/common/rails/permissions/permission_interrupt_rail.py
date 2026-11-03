# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen Permission rail with exact persistence and interrupt marking."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    mark_permission_interrupt_request,
    root_nonpermission_resume_from_context,
    root_permission_resume_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.native_path_context import (
    NativePathGuardProjection, current_native_path_access,
)
from jiuwenswarm.common.utils import logger

ExactPermissionPersistCallback = Callable[..., bool]


_PLAN_HARD_REJECT = object()


def mark_plan_hard_reject(ctx: AgentCallbackContext) -> None:
    setattr(ctx, "_jiuwenswarm_plan_hard_reject", _PLAN_HARD_REJECT)


def plan_hard_rejected(ctx: Any) -> bool:
    return getattr(ctx, "_jiuwenswarm_plan_hard_reject", None) is _PLAN_HARD_REJECT


@dataclass
class _ExactPersistAttempt:
    attempted: bool = False
    approved: bool = False
    session_only: bool = False
    persisted: bool = False
    invalidated: bool = False
    accesses: tuple[tuple[str, str], ...] = ()


_EXACT_PERSIST_ATTEMPT: ContextVar[_ExactPersistAttempt | None] = ContextVar(
    "jiuwenswarm_exact_permission_persist_attempt",
    default=None,
)
_PERMISSION_SNAPSHOT: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "jiuwenswarm_permission_check_snapshot", default=None,
)


class JiuwenSwarmPermissionInterruptRail(PermissionInterruptRail):
    """Permission rail that persists exact rules and marks owned interrupts."""

    def __init__(
        self,
        *args: Any,
        exact_persist_callback: ExactPermissionPersistCallback | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._exact_persist_callback = exact_persist_callback
        self._project_native_path_guard()
        scene_hook = self._host.permission_scene_hook
        if exact_persist_callback is not None and scene_hook is not None:
            async def preserve_confirmation(scene_input: Any) -> Any:
                outcome = await scene_hook(scene_input)
                attempt = _EXACT_PERSIST_ATTEMPT.get()
                if attempt is None or not attempt.approved:
                    return outcome
                if outcome and outcome[0] == "approve":
                    return None
                return outcome
            self._host.permission_scene_hook = preserve_confirmation

    def _project_native_path_guard(self) -> None:
        if getattr(self, "_exact_persist_callback", None) is None:
            return
        # SDK exposes no guard injection API; preserve its checker on each rebuild.
        checker = self._engine._file_guard  # pylint: disable=protected-access
        if checker is not None and not isinstance(checker, NativePathGuardProjection):
            self._engine._file_guard = NativePathGuardProjection(checker)  # pylint: disable=protected-access

    def _sync_engine_workspace_root(self) -> None:
        """Refresh file_guard workspace when the host resolver changes."""
        host = getattr(self, "_host", None)
        resolver = getattr(host, "resolve_workspace_dir", None) if host is not None else None
        if resolver is None:
            return
        try:
            workspace = resolver()
            if workspace is None:
                return
            normalized = Path(workspace).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(
                "[PermissionInterruptRail] workspace_resolve_failed: %s",
                exc,
                exc_info=True,
            )
            return
        engine = self._engine
        current = getattr(engine, "_workspace_root", None)
        if current is not None:
            try:
                if Path(current).expanduser().resolve(strict=False) == normalized:
                    return
            except (OSError, RuntimeError, ValueError) as exc:
                logger.debug(
                    "[PermissionInterruptRail] workspace_compare_failed: %s",
                    exc,
                    exc_info=True,
                )
        engine._workspace_root = normalized  # pylint: disable=protected-access
        engine._rebuild_file_guard()  # pylint: disable=protected-access
        self._project_native_path_guard()

    def update_config(self, *args: Any, **kwargs: Any) -> None:
        self._sync_engine_workspace_root()
        super().update_config(*args, **kwargs)
        self._project_native_path_guard()

    def set_trusted_dirs(self, *args: Any, **kwargs: Any) -> None:
        super().set_trusted_dirs(*args, **kwargs)
        self._project_native_path_guard()

    def _collect_file_guard_persist_accesses(
        self, normalized_name: str, tool_args: dict, permissions_cfg: Any,
    ) -> list[tuple[str, str]]:
        if self._exact_persist_callback is not None and normalized_name == "send_file_to_user":
            from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import (
                SendFilePathGuardEvaluator,
            )
            result = SendFilePathGuardEvaluator().evaluate(
                permissions=permissions_cfg, workspace_root=(
                    self._host.resolve_workspace_dir() if self._host.resolve_workspace_dir else None
                ),
                trusted_dirs=(), raw_paths=tool_args.get("abs_file_path_list"),
            )
            if result.status == "unevaluable" or result.level == "deny":
                raise ValueError(result.reason)
            return list(result.ask_accesses)
        access = current_native_path_access(normalized_name, tool_args)
        if self._exact_persist_callback is not None and access is not None:
            return super()._collect_file_guard_persist_accesses(
                access.guard_tool, access.guard_args, permissions_cfg,
            )
        return super()._collect_file_guard_persist_accesses(normalized_name, tool_args, permissions_cfg)

    def installed_permission_config(self) -> dict[str, Any]:
        return deepcopy(self._static_config)

    def get_permission_snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        """Return the session snapshot through the execution rail's permission hook.

        Args:
            session_id: Session whose effective permissions should be resolved.

        Returns:
            Effective permissions, including invocation permission overlays.

        Raises:
            ValueError: The host cannot provide a permission snapshot.
        """
        snapshot = self._invoke_permissions_hook(
            self._host.get_permissions_snapshot,
            session_id=session_id,
        )
        if not isinstance(snapshot, dict):
            raise ValueError("Installed permission snapshot unavailable")
        return snapshot

    @contextmanager
    def permission_check_context(self, session_id: str | None = None):
        """Share one provider read across this invocation's existing checks."""
        current = _PERMISSION_SNAPSHOT.get()
        if current is not None and current[0] is self:
            yield
            return
        hook = self._host.get_permissions_snapshot
        snapshot = self._invoke_permissions_hook(hook, session_id=session_id) if hook else None
        token = _PERMISSION_SNAPSHOT.set((self, snapshot))
        try:
            yield
        finally:
            _PERMISSION_SNAPSHOT.reset(token)

    def _get_user_input(self, ctx: Any, subject_id: str) -> Any:
        resume = root_permission_resume_from_context(ctx)
        if resume is not None and resume.card.key.tool_call_id == subject_id:
            return resume.user_input
        return super()._get_user_input(ctx, subject_id)

    @staticmethod
    def _approval_arguments_digest(args: dict) -> str:
        return hashlib.sha256(json.dumps(
            args, sort_keys=True, ensure_ascii=True, allow_nan=False,
        ).encode()).hexdigest()

    def bind_approval_request(self, request: Any, tool_call: Any) -> None:
        """Freeze approval scope on the existing pending request, not a store."""
        if self._exact_persist_callback is None or tool_call is None:
            return
        name, args = self._normalize_tool_name(tool_call.name), self.parse_tool_args(tool_call)
        hook = self._host.get_permissions_snapshot
        permissions = self._invoke_permissions_hook(hook) if hook else self._engine.config
        if not isinstance(permissions, dict):
            raise ValueError("permission_snapshot_unavailable")
        request.metadata = {
            **(request.metadata or {}),
            "permission_arguments_sha256": self._approval_arguments_digest(args),
            "permission_ask_accesses": self._collect_file_guard_persist_accesses(
                name, args, permissions,
            ),
        }
        request.auto_confirm_key = ""

    def _invoke_permissions_hook(
        self, hook: Any, *args: Any, session_id: str | None = None,
    ) -> Any:
        if hook is not self._host.get_permissions_snapshot:
            return super()._invoke_permissions_hook(hook, *args, session_id=session_id)
        current = _PERMISSION_SNAPSHOT.get()
        if current is not None and current[0] is self:
            return deepcopy(current[1])
        if self._exact_persist_callback is None:
            snapshot = super()._invoke_permissions_hook(hook, *args, session_id=session_id)
            if isinstance(snapshot, dict):
                from jiuwenswarm.runtime.run_permissions import overlay_run_permissions
                return overlay_run_permissions(snapshot)
            return snapshot
        try:
            snapshot = super()._invoke_permissions_hook(hook, *args, session_id=session_id)
        except Exception:
            snapshot = None
        from jiuwenswarm.runtime.run_permissions import overlay_run_permissions
        return overlay_run_permissions(snapshot) if isinstance(snapshot, dict) else None

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        user_input: Any,
        auto_confirm_config: dict | None = None,
    ) -> Any:
        from jiuwenswarm.runtime.run_permissions import (
            RUN_PERMISSIONS, overlay_run_permissions,
        )

        levels = RUN_PERMISSIONS.get()
        if levels is None:
            levels = getattr(self, "run_permission_levels", None)
        run_token = RUN_PERMISSIONS.set(levels)
        normalized_name = self._normalize_tool_name(tool_call.name) if tool_call else ""
        level = levels.get(normalized_name) if levels else None
        if level == "deny":
            RUN_PERMISSIONS.reset(run_token)
            return self.reject(tool_result="[PERMISSION_DENIED] run policy")
        original_config = self.installed_permission_config() if levels else None
        original_hook = self._host.permission_scene_hook if levels and level == "ask" else None
        if original_config is not None:
            self.update_config(overlay_run_permissions(original_config))
        if original_hook is not None:
            async def preserve_scene_rejection(scene_input: Any) -> Any:
                outcome = await original_hook(scene_input)
                return outcome if outcome is not None and outcome[0] == "reject" else None
            self._host.permission_scene_hook = preserve_scene_rejection
        try:
            return await self._resolve_interrupt_with_run_policy(
                ctx, tool_call, user_input, auto_confirm_config
            )
        finally:
            RUN_PERMISSIONS.reset(run_token)
            if original_hook is not None:
                self._host.permission_scene_hook = original_hook
            if original_config is not None:
                self.update_config(original_config)

    async def _resolve_interrupt_with_run_policy(
        self, ctx: AgentCallbackContext, tool_call: Any, user_input: Any,
        auto_confirm_config: dict | None,
    ) -> Any:
        if self._exact_persist_callback is None:
            return await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
        snapshot_hook = self._host.get_permissions_snapshot
        if snapshot_hook is not None and not isinstance(self._invoke_permissions_hook(snapshot_hook), dict):
            return self.reject(tool_result="[PERMISSION_DENIED] permission_snapshot_unavailable")
        if (
            self._host.get_permissions_snapshot is None
            and tool_call is not None
            and current_native_path_access(tool_call.name, self.parse_tool_args(tool_call)) is not None
        ):
            return self.reject(tool_result="[PERMISSION_DENIED] native_path_snapshot_unavailable")
        payload = self.parse_confirm_payload(user_input)
        attempt = _ExactPersistAttempt()
        token = _EXACT_PERSIST_ATTEMPT.set(attempt)
        try:
            if payload is not None and payload.approved:
                from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS
                if payload.auto_confirm and RUN_PERMISSIONS.get() is not None:
                    return self.reject(tool_result="[PERMISSION_DENIED] run_policy_remember_unsupported")
                resume = root_permission_resume_from_context(ctx)
                request = (
                    resume.card.request if resume is not None
                    else getattr(ctx.extra.get("_interrupt_decision"), "request", None)
                )
                metadata = getattr(request, "metadata", {}) or {}
                args = self.parse_tool_args(tool_call)
                if metadata.get("permission_arguments_sha256") != self._approval_arguments_digest(args):
                    self._remove_exact_auto_confirm(ctx, tool_call)
                    return self.reject(tool_result="[PERMISSION_DENIED] approval_context_changed")
                attempt.accesses = tuple(tuple(item) for item in metadata.get("permission_ask_accesses", ()))
                attempt.approved = True
                attempt.session_only = payload.wants_session_persist()
                try:
                    if not self._exact_persist_callback(
                        self._normalize_tool_name(tool_call.name), args, attempt.accesses,
                        attempt.session_only, True,
                    ):
                        raise ValueError("approval_validation_failed")
                except Exception:
                    self._remove_exact_auto_confirm(ctx, tool_call)
                    return self.reject(tool_result="[PERMISSION_DENIED] approval_invalidated")
            decision = await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
            if attempt.invalidated:
                self._remove_exact_auto_confirm(ctx, tool_call)
                return self.reject(tool_result="[PERMISSION_DENIED] approval_invalidated")
            if payload is not None and payload.wants_permanent_persist() and attempt.persisted:
                self._remove_exact_auto_confirm(ctx, tool_call)
            return decision
        except BaseException:
            if (payload is not None and payload.wants_permanent_persist()) or attempt.attempted:
                self._remove_exact_auto_confirm(ctx, tool_call)
            raise
        finally:
            _EXACT_PERSIST_ATTEMPT.reset(token)

    def _persist_allow_always(
        self, normalized_name: str, tool_args: dict, *, session_id: str | None = None,
    ) -> bool:
        from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS
        if RUN_PERMISSIONS.get() is not None:
            return False
        callback = self._exact_persist_callback
        if callback is None:
            return super()._persist_allow_always(normalized_name, tool_args, session_id=session_id)
        attempt = _EXACT_PERSIST_ATTEMPT.get()
        if attempt is None:
            return False
        attempt.attempted = True
        try:
            attempt.persisted = bool(callback(
                normalized_name, dict(tool_args), attempt.accesses, attempt.session_only,
            ))
            return attempt.persisted
        except Exception:
            attempt.invalidated = True
            return False

    def _persist_session_allow(
        self, normalized_name: str, tool_args: dict, *, session_id: str | None = None,
    ) -> bool:
        if self._exact_persist_callback is None:
            return super()._persist_session_allow(normalized_name, tool_args, session_id=session_id)
        return self._persist_allow_always(normalized_name, tool_args, session_id=session_id)

    def _persist_merged_allow(self, *args: Any, **kwargs: Any) -> bool:
        from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS
        if RUN_PERMISSIONS.get() is not None:
            return False
        return super()._persist_merged_allow(*args, **kwargs)

    def _remove_exact_auto_confirm(self, ctx: Any, tool_call: Any) -> None:
        session = getattr(ctx, "session", None)
        key = self._get_auto_confirm_key(tool_call)
        if session is None or not key:
            return
        config = session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
        if not isinstance(config, dict) or key not in config:
            return
        updated = dict(config)
        updated.pop(key, None)
        session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: updated})

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if plan_hard_rejected(ctx) or root_nonpermission_resume_from_context(ctx) is not None:
            return
        self._sync_engine_workspace_root()
        try:
            await super().before_tool_call(ctx)
        except AbortError as exc:
            cause = getattr(exc, "cause", None)
            request = getattr(cause, "request", None)
            if request is not None:
                self.bind_approval_request(request, ctx.inputs.tool_call)
                mark_permission_interrupt_request(ctx, request)
            raise


__all__ = ["JiuwenSwarmPermissionInterruptRail"]
