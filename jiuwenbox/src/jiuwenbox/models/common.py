# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
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
    # Single event per operation, emitted **after** the call returns so
    # the payload carries both intent (command/workdir) and outcome
    # (exit_code, stdout/stderr, duration, error). The earlier "intent
    # only" event was dropped because it doubled the JSONL volume
    # without adding information.
    EXEC_COMMAND = "exec_command"
    KILL_BACKGROUND_JOB = "kill_background_job"
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
