# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Sandbox data models."""

from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

SANDBOX_ID_MIN_LEN = 4
SANDBOX_ID_MAX_LEN = 40
CUSTOM_SANDBOX_ID_RE = re.compile(
    rf"^[0-9a-z_-]{{{SANDBOX_ID_MIN_LEN},{SANDBOX_ID_MAX_LEN}}}$"
)

SANDBOX_ID_FORMAT_MESSAGE = (
    f"sandbox_id must be {SANDBOX_ID_MIN_LEN}-{SANDBOX_ID_MAX_LEN} characters "
    "and contain only lowercase letters, digits, hyphens, and underscores "
    "(e.g. my-sb_01)"
)


class InvalidSandboxIdError(Exception):
    """Raised when a user-supplied sandbox_id fails format validation."""


JOB_ID_MIN_LEN = 4
JOB_ID_MAX_LEN = 40
CUSTOM_JOB_ID_RE = re.compile(
    rf"^[0-9a-z_-]{{{JOB_ID_MIN_LEN},{JOB_ID_MAX_LEN}}}$"
)

JOB_ID_FORMAT_MESSAGE = (
    f"job_id must be {JOB_ID_MIN_LEN}-{JOB_ID_MAX_LEN} characters and contain "
    "only lowercase letters, digits, hyphens, and underscores (e.g. http-srv)"
)


class InvalidJobIdError(Exception):
    """Raised when a user-supplied job_id fails format validation."""


def generate_sandbox_id() -> str:
    """Generate a sandbox id using the existing uuid4[:12] scheme."""
    return str(uuid.uuid4())[:12]


def validate_custom_sandbox_id(value: str) -> str:
    """Validate a user-supplied sandbox_id; return it unchanged on success."""
    if not CUSTOM_SANDBOX_ID_RE.fullmatch(value):
        raise InvalidSandboxIdError(SANDBOX_ID_FORMAT_MESSAGE)
    return value


def generate_job_id() -> str:
    return str(uuid.uuid4())[:12]


def validate_custom_job_id(value: str) -> str:
    if not CUSTOM_JOB_ID_RE.fullmatch(value):
        raise InvalidJobIdError(JOB_ID_FORMAT_MESSAGE)
    return value


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

    env: dict[str, str] = Field(default_factory=dict)
    sandbox_id: str | None = None


class SandboxRef(BaseModel):
    """Reference to an existing sandbox."""

    id: str
    phase: SandboxPhase = SandboxPhase.PROVISIONING
    runtime: str = "process"
    pid: int | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    last_active_at: datetime | None = None
    error_message: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ExecResult(BaseModel):
    """Result of executing a command in a sandbox."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class BackgroundExecRequest(BaseModel):
    command: list[str]
    job_id: str | None = None
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin: str | None = None
    timeout_seconds: int | None = None


class BackgroundExecResult(BaseModel):
    started: bool
    job_id: str | None = None
    pid: int | None = None
    command: list[str] = Field(default_factory=list)
    running: bool | None = None
    exit_code: int | None = None
    error_message: str | None = None


class BackgroundJobSummary(BaseModel):
    job_id: str
    pid: int | None
    command: list[str]
    running: bool
    exit_code: int | None
    started_at: datetime
    finished_at: datetime | None


class BackgroundJobStatus(BaseModel):
    job_id: str
    sandbox_id: str
    command: list[str]
    pid: int | None
    running: bool
    exit_code: int | None
    started_at: datetime
    finished_at: datetime | None
    # Background jobs discard stdout/stderr; fields stay for API stability.
    stdout: str = ""
    stderr: str = ""
    workdir: str | None = None


class KillBackgroundJobRequest(BaseModel):
    signal: int = 15


class KillBackgroundJobResult(BaseModel):
    job_id: str
    killed: bool
    reason: str
    exit_code: int | None = None
