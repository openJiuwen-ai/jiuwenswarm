"""Sandbox data models."""

from __future__ import annotations

import enum
from datetime import datetime
from pydantic import BaseModel, Field


class SandboxPhase(str, enum.Enum):
    PROVISIONING = "provisioning"
    READY = "ready"
    STOPPED = "stopped"
    ERROR = "error"
    DELETING = "deleting"


class PolicyMode(str, enum.Enum):
    OVERRIDE = "override"
    APPEND = "append"


class SandboxSpec(BaseModel):
    """Specification for creating a sandbox."""

    workdir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class SandboxRef(BaseModel):
    """Reference to an existing sandbox."""

    id: str
    phase: SandboxPhase = SandboxPhase.PROVISIONING
    runtime: str = "process"
    pid: int | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    error_message: str | None = None
    command: list[str] = Field(default_factory=list)
    workdir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ExecResult(BaseModel):
    """Result of executing a command in a sandbox."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
