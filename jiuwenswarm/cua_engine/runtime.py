"""Embed the engine's owned execution session without an internal MCP server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import weakref

from mcp.types import CallToolResult

from .hosting import prepare_native


class DesktopLane:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.blocker: str | None = None

    @asynccontextmanager
    async def acquire(self):
        async with self.lock:
            if self.blocker:
                raise RuntimeError("Desktop cleanup is unconfirmed: " + self.blocker)
            yield


_LANES = weakref.WeakKeyDictionary()


def desktop_lane() -> DesktopLane:
    loop = asyncio.get_running_loop()
    if loop not in _LANES:
        _LANES[loop] = DesktopLane()
    return _LANES[loop]


class ReplRuntime:
    """One persistent engine session per delegation; the engine owns its worker."""

    def __init__(self, *, session=None):
        # Align the embedded REPL with the host's single-step desktop tools.
        os.environ.setdefault("COMPUTER_USE_DESKTOP", "exclusive")
        self.session = session
        self.call_lock = asyncio.Lock()
        self.close_task = None
        self.closing = False
        self.cleanup_error = None

    async def execute(self, code, title=""):
        async with self.call_lock:
            if self.closing or self.cleanup_error:
                raise RuntimeError("The previous REPL was closed; recreate variables in a fresh delegation.")
            try:
                if self.session is None:
                    prepare_native()
                    from computer_use.mcp.session import ExecutionSession

                    self.session = ExecutionSession()
                # No retained-history capability is advertised: reads stay full.
                result = await self.session.execute(code, title=title.strip() or None)
                return CallToolResult.model_validate(result)
            except BaseException:
                # The engine cancels and retires its own worker before returning.
                # Closing this wrapper prevents queued calls from reusing it.
                await asyncio.shield(self.close())
                raise

    def request_close(self):
        self.closing = True

    async def _close(self):
        try:
            if self.session is not None:
                result = await self.session.close()
                if result.get("isError"):
                    detail = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
                    raise RuntimeError(detail or "The engine could not confirm native cleanup.")
        except Exception as error:
            self.cleanup_error = str(error) or type(error).__name__
            desktop_lane().blocker = self.cleanup_error
            raise RuntimeError(
                "Native cleanup is unconfirmed; desktop input remains blocked: " + self.cleanup_error
            ) from error

    async def close(self):
        self.request_close()
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="cua-repl-cleanup")
        await asyncio.shield(self.close_task)
