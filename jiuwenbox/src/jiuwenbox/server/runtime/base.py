# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Abstract base class for sandbox runtime adapters."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenbox.models.sandbox import (
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobResult,
)


@dataclass(frozen=True)
class RuntimeExecRequest:
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class RuntimeBackgroundExecRequest:
    command: list[str]
    job_id: str
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None


@dataclass(frozen=True)
class RuntimeFileOpResult:
    """Result of an in-sandbox file op routed via the daemon fast path.

    ``ok`` indicates whether the op completed successfully. When ``ok`` is
    ``False`` callers can branch on ``error`` (a stable string code such
    as ``"not_found"``, ``"is_directory"``, ``"is_symlink"``, etc.) and
    optionally on ``errno`` to translate into HTTP semantics. ``content``
    and ``items`` are populated by the read/list paths respectively.
    """

    ok: bool
    error: str | None = None
    errno: int | None = None
    detail: str | None = None
    content: bytes | None = None
    items: list[dict[str, Any]] | None = None


class RuntimeAdapter(abc.ABC):
    """Interface for sandbox runtime backends (process, docker, etc.)."""

    @abc.abstractmethod
    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int:
        """Create and start a sandboxed process.  Returns the OS pid."""
        ...

    @abc.abstractmethod
    async def stop(self, sandbox_id: str, timeout: float = 10.0) -> None:
        """Gracefully stop a sandbox."""
        ...

    @abc.abstractmethod
    async def is_running(self, sandbox_id: str) -> bool:
        """Check if the sandbox process is still alive."""
        ...

    @abc.abstractmethod
    async def exec(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        """Execute a one-shot command inside a running sandbox."""
        ...

    @abc.abstractmethod
    async def exec_background(
        self,
        sandbox_id: str,
        request: RuntimeBackgroundExecRequest,
    ) -> BackgroundExecResult:
        """Start a background command inside a running sandbox."""
        ...

    @abc.abstractmethod
    async def get_background_job(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJobStatus:
        ...

    @abc.abstractmethod
    async def list_background_jobs(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[BackgroundJobSummary]:
        ...

    @abc.abstractmethod
    async def kill_background_job(
        self,
        sandbox_id: str,
        job_id: str,
        signum: int = 15,
    ) -> KillBackgroundJobResult:
        ...

    @abc.abstractmethod
    async def cleanup(self, sandbox_id: str) -> None:
        """Release all resources for a sandbox."""
        ...

    async def write_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
        mode: int | None = None,
    ) -> RuntimeFileOpResult:
        """Write ``content`` to ``sandbox_path`` inside the sandbox.

        Default implementation indicates the runtime does not provide a
        fast path and the caller should fall back to ``exec``. Subclasses
        with a daemon IPC channel (``ProcessRuntime``) override this to
        avoid the fork+exec roundtrip.
        """
        return RuntimeFileOpResult(
            ok=False,
            error="unsupported",
            detail="runtime adapter does not implement write_file fast path",
        )

    async def read_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> RuntimeFileOpResult:
        """Read ``sandbox_path`` from the sandbox via the daemon fast path."""
        return RuntimeFileOpResult(
            ok=False,
            error="unsupported",
            detail="runtime adapter does not implement read_file fast path",
        )

    async def list_dir(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> RuntimeFileOpResult:
        """List directory entries under ``sandbox_path``."""
        return RuntimeFileOpResult(
            ok=False,
            error="unsupported",
            detail="runtime adapter does not implement list_dir fast path",
        )
