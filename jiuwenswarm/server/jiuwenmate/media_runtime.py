"""Execution-local media access and cancellation, independent of channel APIs."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol, TypeVar

SCOPE_KEY = "jiuwenmate.execution_media"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_T = TypeVar("_T")


class InboundMedia(Protocol):
    """A channel adapter owns verified inbound files and fresh source checks."""

    async def resolve(self, reference: str) -> str: ...
    def guidance(self) -> str: ...
    async def aclose(self) -> None: ...


class MediaExecution(Protocol):
    """Cancellation state owned by the execution driver."""

    media_tasks: set[asyncio.Task]

    @property
    def reason(self) -> str | None: ...


class ExecutionMediaScope:
    """Permit current inbound media and this session's generated output images.

    Session output remains readable after a permission pause or process restart.
    Inbound directories are supplied by the host, never inferred from user text.
    """

    def __init__(self, session_workspace: Path, *, inbound_directory: Path | None = None) -> None:
        self.session = session_workspace.resolve()
        self.output = self.session / "output"
        self.inbound: InboundMedia | None = None
        self.source_guard: Callable[[], Awaitable[None]] | None = None
        self._inbound_directory = inbound_directory
        self._closed = False

    async def resolve(self, reference: str) -> str:
        if self._closed or not isinstance(reference, str) or not reference.strip() or "\x00" in reference:
            raise ValueError("本次执行的媒体已不可读或图片路径无效")
        if reference.startswith(("media:", "feishu-image:")):
            if self.inbound is None:
                raise ValueError("本次执行没有该入站图片的授权")
            return await self.inbound.resolve(reference)
        if self.source_guard is not None:
            await self.source_guard()
        candidate = Path(reference)
        if not candidate.is_absolute():
            # Bash and existing office tools also return office-relative paths.
            office_relative = candidate.parts[0] in {"sessions", "incoming"}
            candidate = (self.session.parent.parent if office_relative else self.session) / candidate
        resolved = candidate.resolve()
        output_allowed = (not self.output.is_symlink()
                          and self.output.resolve().is_relative_to(self.session)
                          and resolved.is_relative_to(self.output.resolve()))
        inbound_allowed = False
        if self._inbound_directory is not None:
            directory = self._inbound_directory
            inbound_allowed = (not directory.is_symlink()
                               and directory.resolve().is_relative_to(self.session.parent.parent / "incoming")
                               and resolved.is_relative_to(directory.resolve()))
        if candidate.is_symlink() or not (output_allowed or inbound_allowed):
            raise ValueError("只能读取本次消息的媒体或当前会话 output/ 中的截图")
        if not resolved.is_file() or resolved.suffix.lower() not in _IMAGE_SUFFIXES:
            raise ValueError("图片文件不存在或格式不支持")
        return str(resolved)

    async def aclose(self) -> None:
        self._closed = True
        if self.inbound is not None:
            await self.inbound.aclose()


async def run_media_stage(operation: Coroutine[Any, Any, _T], handle: MediaExecution) -> _T:
    """Join a media subtask on stop while preserving the driver's final cleanup."""
    task = asyncio.create_task(operation)
    handle.media_tasks.add(task)
    try:
        if handle.reason is not None:
            task.cancel()
        return await task
    finally:
        handle.media_tasks.discard(task)
