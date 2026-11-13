"""Embed the engine's owned execution session without an internal MCP server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import weakref

from filelock import FileLock, Timeout as FileLockTimeout
from mcp.types import CallToolResult, TextContent

from jiuwenswarm.common.cua_config import cua_repl_unavailable_reason


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


def _prepare_native_driver() -> None:
    reason = cua_repl_unavailable_reason()
    if reason:
        raise RuntimeError(f"Computer Use native backend is unavailable ({reason}).")

    from computer_use.paths import native_driver, repository_root
    from computer_use_native_tools.native import build_native

    root = repository_root()
    executable = Path(native_driver(root=root)["command"])
    incomplete = root / ".jiuwenswarm-build-incomplete"
    if executable.is_file() and not incomplete.exists():
        return
    with FileLock(root / ".jiuwenswarm-build.lock", timeout=300):
        if executable.is_file() and not incomplete.exists():
            return
        incomplete.touch()
        build_native(root=root)
        if not executable.is_file():
            raise RuntimeError("Computer Use did not produce a native driver.")
        incomplete.unlink()


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
                    from computer_use_mcp.session import ExecutionSession

                    if not getattr(sys, "frozen", False):
                        try:
                            await asyncio.to_thread(_prepare_native_driver)
                        except FileLockTimeout:
                            message = (
                                "Another session is preparing the desktop driver. "
                                "No REPL code was executed. Retry after the build finishes."
                            )
                            return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
                        except Exception as error:
                            reason = str(error).strip() or type(error).__name__
                            message = (
                                f"Failed to prepare the native desktop driver. {reason}\n"
                                "No REPL code was executed."
                            )
                            return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
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
