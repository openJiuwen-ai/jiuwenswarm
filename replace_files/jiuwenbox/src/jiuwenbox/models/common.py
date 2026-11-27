"""Common data models shared across components."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class AuditEventType(str, enum.Enum):
    SANDBOX_CREATED = "sandbox_created"
    SANDBOX_STARTED = "sandbox_started"
    SANDBOX_STOPPED = "sandbox_stopped"
    SANDBOX_DELETED = "sandbox_deleted"
    POLICY_APPLIED = "policy_applied"
    EXEC_COMMAND = "exec_command"
    FILE_TRANSFER = "file_transfer"


class AuditEvent(BaseModel):
    """Structured audit log entry."""

    timestamp: datetime = Field(default_factory=datetime.now)
    event_type: AuditEventType
    sandbox_id: str
    details: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str
    runtime: str = "process"
    landlock_supported: bool = False
    sandboxes_active: int = 0
