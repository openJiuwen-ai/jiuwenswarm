# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen Permission rail with exact persistence and interrupt marking."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    mark_permission_interrupt_request,
    root_nonpermission_resume_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.native_path_context import (
    NativePathGuardProjection, current_native_path_access,
)

ExactPermissionPersistCallback = Callable[
    [str, dict[str, Any], tuple[tuple[str, str], ...]], bool
]


@dataclass
class _ExactPersistAttempt:
    attempted: bool = False


_EXACT_PERSIST_ATTEMPT: ContextVar[_ExactPersistAttempt | None] = ContextVar(
    "jiuwenswarm_exact_permission_persist_attempt",
    default=None,
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

    def _project_native_path_guard(self) -> None:
        if getattr(self, "_exact_persist_callback", None) is None:
            return
        # SDK exposes no guard injection API; preserve its checker on each rebuild.
        checker = self._engine._file_guard  # pylint: disable=protected-access
        if checker is not None and not isinstance(checker, NativePathGuardProjection):
            self._engine._file_guard = NativePathGuardProjection(checker)  # pylint: disable=protected-access

    def update_config(self, *args: Any, **kwargs: Any) -> None:
        super().update_config(*args, **kwargs)
        self._project_native_path_guard()

    def set_trusted_dirs(self, *args: Any, **kwargs: Any) -> None:
        super().set_trusted_dirs(*args, **kwargs)
        self._project_native_path_guard()

    def _collect_file_guard_persist_accesses(
        self, normalized_name: str, tool_args: dict, permissions_cfg: Any,
    ) -> list[tuple[str, str]]:
        access = current_native_path_access(normalized_name, tool_args)
        if self._exact_persist_callback is not None and access is not None:
            return super()._collect_file_guard_persist_accesses(
                access.guard_tool, access.guard_args, permissions_cfg,
            )
        return super()._collect_file_guard_persist_accesses(normalized_name, tool_args, permissions_cfg)

    def installed_permission_config(self) -> dict[str, Any]:
        return deepcopy(self._static_config)

    def _invoke_permissions_hook(
        self, hook: Any, *args: Any, session_id: str | None = None,
    ) -> Any:
        if self._exact_persist_callback is None or hook is not self._host.get_permissions_snapshot:
            return super()._invoke_permissions_hook(hook, *args, session_id=session_id)
        # Preserve the SDK's installed-config fallback, but return it through
        # the rail update path so a rebuild cannot discard native extraction.
        try:
            snapshot = super()._invoke_permissions_hook(hook, *args, session_id=session_id)
        except Exception:
            snapshot = None
        return snapshot if isinstance(snapshot, dict) else self.installed_permission_config()

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        user_input: Any,
        auto_confirm_config: dict | None = None,
    ) -> Any:
        if self._exact_persist_callback is None:
            return await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
        if (
            self._host.get_permissions_snapshot is None
            and tool_call is not None
            and current_native_path_access(tool_call.name, self.parse_tool_args(tool_call)) is not None
        ):
            return self.reject(tool_result="[PERMISSION_DENIED] native_path_snapshot_unavailable")
        payload = self.parse_confirm_payload(user_input)
        permanent = bool(
            payload is not None
            and payload.approved
            and payload.auto_confirm
            and payload.persist_allow
        )
        attempt = _ExactPersistAttempt()
        token = _EXACT_PERSIST_ATTEMPT.set(attempt)
        try:
            return await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
        except BaseException:
            if permanent or attempt.attempted:
                self._remove_exact_auto_confirm(ctx, tool_call)
            raise
        finally:
            _EXACT_PERSIST_ATTEMPT.reset(token)

    def _persist_allow_always(
        self, normalized_name: str, tool_args: dict, *, session_id: str | None = None,
    ) -> bool:
        callback = self._exact_persist_callback
        if callback is None:
            return super()._persist_allow_always(normalized_name, tool_args, session_id=session_id)
        attempt = _EXACT_PERSIST_ATTEMPT.get()
        if attempt is None:
            return False
        attempt.attempted = True
        accesses = tuple(
            self._collect_file_guard_persist_accesses(
                normalized_name, tool_args, self._engine.config
            )
        )
        try:
            return bool(callback(normalized_name, dict(tool_args), accesses))
        except Exception:
            return False

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
        if root_nonpermission_resume_from_context(ctx) is not None:
            return
        try:
            await super().before_tool_call(ctx)
        except AbortError as exc:
            cause = getattr(exc, "cause", None)
            request = getattr(cause, "request", None)
            if request is not None:
                mark_permission_interrupt_request(ctx, request)
            raise


__all__ = ["JiuwenSwarmPermissionInterruptRail"]
