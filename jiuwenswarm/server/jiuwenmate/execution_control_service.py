"""Stop and cancel controls on one Request.

Only the principal that sent a Request may control it. The first control of a
Request is stored before anything is interrupted, so the fact survives a failed
interruption or cleanup; a repeated control returns the stored fact and
interrupts nothing again. The scheduler ends only the target Request. A Request
that carries a control is never continued automatically (JM-13).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from jiuwenswarm.server.jiuwenmate.execution_driver import AbortReason
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.session_scheduler import SessionScheduler
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

ControlStatus = Literal["accepted", "failed"]
_REASONS = ("stop", "cancel")
_DETAIL_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_controls (
    namespace_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    principal_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, request_id)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, kw_only=True)
class RequestControl:
    """``accepted``: the Request was closed or its interruption requested; ``failed``: interrupting it raised."""

    request_id: str
    reason: AbortReason
    principal_ref: str
    status: ControlStatus
    detail: str | None
    requested_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "reason": self.reason,
            "status": self.status,
            "detail": self.detail,
            "requested_at": self.requested_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ControlStore(SQLiteStore):
    _schema = _SCHEMA

    @staticmethod
    def _control(row) -> RequestControl:
        return RequestControl(
            request_id=row["request_id"],
            reason=row["reason"],
            principal_ref=row["principal_ref"],
            status=row["status"],
            detail=row["detail"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row(self, conn, namespace_id: str, request_id: str):
        return conn.execute(
            "SELECT * FROM request_controls WHERE namespace_id = ? AND request_id = ?", (namespace_id, request_id)
        ).fetchone()

    def claim(
        self, namespace_id: str, request_id: str, *, reason: AbortReason, principal_ref: str, now: datetime
    ) -> tuple[RequestControl, bool]:
        """Store the first control of a Request; ``True`` when this call stored it."""
        with self._transaction() as conn:
            created = conn.execute(
                "INSERT OR IGNORE INTO request_controls VALUES (?, ?, ?, ?, 'accepted', NULL, ?, ?)",
                (namespace_id, request_id, reason, principal_ref, now.isoformat(), now.isoformat()),
            ).rowcount
            row = self._row(conn, namespace_id, request_id)
        return self._control(row), bool(created)

    def fail(self, namespace_id: str, request_id: str, detail: str, *, now: datetime) -> RequestControl:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE request_controls SET status = 'failed', detail = ?, updated_at = ? "
                "WHERE namespace_id = ? AND request_id = ?",
                (detail[:_DETAIL_CHARS], now.isoformat(), namespace_id, request_id),
            )
            row = self._row(conn, namespace_id, request_id)
        return self._control(row)

    def get(self, namespace_id: str, request_id: str) -> RequestControl | None:
        with self._transaction(write=False) as conn:
            row = self._row(conn, namespace_id, request_id)
        return None if row is None else self._control(row)


class ExecutionControlService:
    def __init__(
        self,
        sessions: ExecutionRepository,
        scheduler: SessionScheduler,
        controls: ControlStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._scheduler = scheduler
        self._controls = controls
        self._clock = clock

    async def control(self, namespace_id: str, request_id: str, *, reason: str, principal_ref: str) -> RequestControl:
        """Stop or cancel the Request for its sender; a Request that already ended without a control is refused."""
        require_identifier("request_id", request_id)
        if reason not in _REASONS:
            raise PersistentAgentError("reason must be 'stop' or 'cancel'")
        state = await asyncio.to_thread(self._sessions.get_request, namespace_id, request_id)
        if principal_ref != state.record.access.principal_ref:
            raise PersistentAgentError("only the sender may control this request", code="FORBIDDEN")
        existing = await asyncio.to_thread(self._controls.get, namespace_id, request_id)
        if existing is not None:
            return existing
        if state.status == "ended":
            raise PersistentAgentError(f"request already ended as {state.outcome}", code="CONFLICT")
        control, created = await asyncio.to_thread(
            self._controls.claim,
            namespace_id,
            request_id,
            reason=reason,
            principal_ref=principal_ref,
            now=self._clock(),
        )
        if not created:
            return control
        try:
            await self._scheduler.abort(namespace_id, request_id, reason)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] request control failed: request=%s reason=%s error=%s", request_id, reason, exc
            )
            return await asyncio.to_thread(
                self._controls.fail, namespace_id, request_id, str(exc) or type(exc).__name__, now=self._clock()
            )
        return control

    def control_of(self, namespace_id: str, request_id: str) -> RequestControl | None:
        return self._controls.get(namespace_id, request_id)
