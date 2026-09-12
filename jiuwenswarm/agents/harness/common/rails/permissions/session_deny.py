# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-scoped denial records for auto permissions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    DENY_LEVEL,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
)


@dataclass(frozen=True)
class SessionDenyRecord:
    """A user denial scoped to one session and one normalized payload hash."""

    session_id: str
    tool_name: str
    normalized_args_hash: str
    reason: str


class SessionDenyStore:
    """In-memory session-local deny store for auto permission decisions."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], SessionDenyRecord] = {}

    def record_denial(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_args: Mapping[str, Any],
        reason: str,
    ) -> None:
        """Record a user denial for the descriptor payload."""
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return
        key = _build_key(normalized_session_id, tool_name, tool_args)
        fingerprint = _args_fingerprint(tool_args)
        self._records[key] = SessionDenyRecord(
            session_id=normalized_session_id,
            tool_name=tool_name,
            normalized_args_hash=fingerprint,
            reason=reason,
        )

    def matches(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_args: Mapping[str, Any],
    ) -> bool:
        """Return whether the same session rejected this payload."""
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return False
        return _build_key(normalized_session_id, tool_name, tool_args) in self._records


def evaluate_session_deny(
    store: SessionDenyStore,
    *,
    session_id: str | None,
    tool_name: str,
    tool_args: Mapping[str, Any],
) -> DecisionRoute | None:
    """Return deny when a session-local user rejection matches the payload."""
    if session_id is None:
        return None
    if not store.matches(
        session_id=session_id,
        tool_name=tool_name,
        tool_args=tool_args,
    ):
        return None
    return DecisionRoute(
        level=DENY_LEVEL,
        reason="session_user_denied",
        source="session_deny",
    )


def _build_key(
    session_id: str,
    tool_name: str,
    tool_args: Mapping[str, Any],
) -> tuple[str, str, str]:
    return (session_id, tool_name, _args_fingerprint(tool_args))


def _args_fingerprint(tool_args: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(tool_args),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
