# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human approval scopes for file delivery path decisions."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import (
    FileGuardReadAccess,
)

logger = logging.getLogger(__name__)

HumanApprovalScope = Literal["allow_once", "session", "permanent", "reject"]
ExactPermissionPersistCallback = Callable[
    [str, dict[str, Any], tuple[tuple[str, str], ...]],
    bool,
]


@dataclass(frozen=True)
class SendFileApprovalOutcome:
    """Result of applying a validated human approval scope."""

    scope: HumanApprovalScope
    remembered: bool = False
    persisted: bool = False
    reason: str = ""


class SendFileSessionApprovalStore:
    """Owner/session/tool-scoped exact read approvals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approvals: dict[tuple[str, str, str], set[FileGuardReadAccess]] = {}

    def remember(
        self,
        *,
        owner_scope: str,
        session_id: str | None,
        tool_name: str,
        accesses: tuple[FileGuardReadAccess, ...],
    ) -> bool:
        key = _approval_key(owner_scope, session_id, tool_name)
        normalized = _normalize_read_accesses(accesses)
        if key is None or not normalized:
            return False
        with self._lock:
            self._approvals.setdefault(key, set()).update(normalized)
        return True

    def matches(
        self,
        *,
        owner_scope: str,
        session_id: str | None,
        tool_name: str,
        accesses: tuple[FileGuardReadAccess, ...],
    ) -> bool:
        key = _approval_key(owner_scope, session_id, tool_name)
        normalized = _normalize_read_accesses(accesses)
        if key is None or not normalized:
            return False
        with self._lock:
            approved = self._approvals.get(key, set())
            return normalized.issubset(approved)

    def clear_session(self, *, owner_scope: str, session_id: str | None) -> None:
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            return
        normalized_owner = str(owner_scope or "").strip()
        with self._lock:
            for key in tuple(self._approvals):
                if key[:2] == (normalized_owner, normalized_session):
                    self._approvals.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._approvals.clear()


class SendFileHumanApprovalBridge:
    """Apply human session/permanent choices using only public APIs."""

    def __init__(
        self,
        *,
        session_store: SendFileSessionApprovalStore,
        exact_persist_callback: ExactPermissionPersistCallback | None = None,
    ) -> None:
        self._session_store = session_store
        self._exact_persist_callback = exact_persist_callback

    @property
    def session_store(self) -> SendFileSessionApprovalStore:
        return self._session_store

    def apply(
        self,
        *,
        scope: HumanApprovalScope,
        owner_scope: str,
        session_id: str | None,
        tool_name: str,
        tool_args: Mapping[str, Any],
        ask_accesses: tuple[FileGuardReadAccess, ...],
    ) -> SendFileApprovalOutcome:
        """Remember only validated human scopes; current-call approval is external."""

        if scope in {"allow_once", "reject"}:
            return SendFileApprovalOutcome(scope=scope)
        accesses = _ordered_read_accesses(ask_accesses)
        if not accesses:
            return SendFileApprovalOutcome(
                scope=scope,
                reason="send_file_approval_accesses_unevaluable",
            )
        if scope == "session":
            remembered = self._session_store.remember(
                owner_scope=owner_scope,
                session_id=session_id,
                tool_name=tool_name,
                accesses=accesses,
            )
            return SendFileApprovalOutcome(
                scope=scope,
                remembered=remembered,
                reason="" if remembered else "send_file_session_scope_unavailable",
            )
        return self._persist_permanent(
            tool_name=tool_name,
            tool_args=tool_args,
            accesses=accesses,
        )

    def _persist_permanent(
        self,
        *,
        tool_name: str,
        tool_args: Mapping[str, Any],
        accesses: tuple[FileGuardReadAccess, ...],
    ) -> SendFileApprovalOutcome:
        callback = self._exact_persist_callback
        if callback is None:
            return SendFileApprovalOutcome(
                scope="permanent",
                reason="send_file_permanent_scope_unavailable",
            )

        try:
            persisted = bool(callback(tool_name, dict(tool_args), tuple(accesses)))
            if not persisted:
                raise RuntimeError("exact persistence returned false")
        except Exception:
            logger.exception("[SendFileApproval] permanent approval failed")
            return SendFileApprovalOutcome(
                scope="permanent",
                reason="send_file_permanent_persist_failed",
            )
        return SendFileApprovalOutcome(
            scope="permanent",
            remembered=True,
            persisted=True,
        )


def parse_human_approval_scope(payload: Mapping[str, Any]) -> HumanApprovalScope | None:
    """Map the four boolean confirmation aliases to one human scope."""

    approved = payload.get("approved")
    auto_confirm = payload.get("auto_confirm", False)
    persist_allow = payload.get("persist_allow", False)
    if not all(isinstance(value, bool) for value in (approved, auto_confirm, persist_allow)):
        return None
    if not approved:
        return "reject"
    if not auto_confirm:
        return "allow_once" if not persist_allow else None
    return "permanent" if persist_allow else "session"


def _approval_key(
    owner_scope: str,
    session_id: str | None,
    tool_name: str,
) -> tuple[str, str, str] | None:
    normalized_session = str(session_id or "").strip()
    normalized_tool = str(tool_name or "").strip()
    if not normalized_session or not normalized_tool:
        return None
    return str(owner_scope or "").strip(), normalized_session, normalized_tool


def _normalize_read_accesses(
    accesses: tuple[FileGuardReadAccess, ...],
) -> frozenset[FileGuardReadAccess]:
    return frozenset(
        (path, "read")
        for path, action in accesses
        if isinstance(path, str) and path.strip() and action == "read"
    )


def _ordered_read_accesses(
    accesses: tuple[FileGuardReadAccess, ...],
) -> tuple[FileGuardReadAccess, ...]:
    ordered: list[FileGuardReadAccess] = []
    seen: set[FileGuardReadAccess] = set()
    for path, action in accesses:
        access = (path, "read")
        if not isinstance(path, str) or not path.strip():
            continue
        if action != "read" or access in seen:
            continue
        seen.add(access)
        ordered.append(access)
    return tuple(ordered)


__all__ = [
    "HumanApprovalScope",
    "SendFileApprovalOutcome",
    "SendFileHumanApprovalBridge",
    "SendFileSessionApprovalStore",
    "parse_human_approval_scope",
]
