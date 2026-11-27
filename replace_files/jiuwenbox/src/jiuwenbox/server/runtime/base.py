"""Abstract base class for sandbox runtime adapters."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from jiuwenbox.models.sandbox import ExecResult


@dataclass(frozen=True)
class RuntimeExecRequest:
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None
    timeout: float | None = None


class RuntimeAdapter(abc.ABC):
    """Interface for sandbox runtime backends (process, docker, etc.)."""

    @abc.abstractmethod
    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        command: list[str],
        workdir: str | None = None,
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
    async def cleanup(self, sandbox_id: str) -> None:
        """Release all resources for a sandbox."""
        ...
