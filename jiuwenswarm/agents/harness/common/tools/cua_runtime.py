"""Embed the engine's owned execution session without an internal MCP server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import weakref

from mcp.types import CallToolResult


class DesktopLane:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.blockers: dict[object, str] = {}

    @property
    def blocker(self) -> str | None:
        return "; ".join(self.blockers.values()) or None

    def block(self, owner: object, detail: str):
        self.blockers[owner] = detail

    def clear_blocker(self, owner: object):
        self.blockers.pop(owner, None)

    @asynccontextmanager
    async def acquire(self, *, recovery_owner=None):
        async with self.lock:
            # Independent failed owners must be able to clean up without
            # waiting for each other. Recovery can only clear its own entry.
            if self.blockers and recovery_owner not in self.blockers:
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
        failed = False
        try:
            async with self.call_lock:
                if self.closing:
                    raise RuntimeError("The previous REPL was closed; recreate variables in a fresh delegation.")
                if self.cleanup_error:
                    raise RuntimeError("Native cleanup is unconfirmed; retry cua_repl_reset before continuing.")
                try:
                    if self.session is None:
                        from computer_use_mcp.session import ExecutionSession

                        self.session = ExecutionSession()
                    # Ordinary code errors preserve the worker and its variables.
                    result = await self.session.execute(code, title=title.strip() or None)
                    return CallToolResult.model_validate(result)
                except BaseException:
                    self.request_close()
                    failed = True
                    raise
        except BaseException:
            if failed:
                # Release call_lock before cleanup; queued calls see closing.
                await asyncio.shield(self.close())
            raise

    async def reset(self):
        async with self.call_lock:
            if self.closing:
                raise RuntimeError("The previous REPL was closed; recreate variables in a fresh delegation.")
            if self.session is None:
                return CallToolResult(content=[{"type": "text", "text": "No active REPL session to reset."}])
            # Block input until the SDK confirms cleanup, even if this caller is cancelled.
            desktop_lane().block(self, "REPL reset is awaiting native cleanup.")
            task = asyncio.create_task(self._reset(), name="cua-repl-reset")
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                try:
                    await asyncio.shield(task)
                finally:
                    # _reset records cleanup failures in the lane; they must
                    # not turn task cancellation into an ordinary tool error.
                    raise cancelled

    async def _reset(self):
        try:
            result = CallToolResult.model_validate(await self.session.reset())
            if result.isError:
                detail = "\n".join(part.text for part in result.content if part.type == "text")
                self._block_cleanup(detail or "The engine could not confirm native cleanup.")
            elif not self.closing:
                self.cleanup_error = None
                desktop_lane().clear_blocker(self)
            return result
        except Exception as error:
            self._block_cleanup(str(error) or type(error).__name__)
            raise RuntimeError("Native cleanup is unconfirmed; retry cua_repl_reset: " + self.cleanup_error) from error

    def _block_cleanup(self, detail):
        self.cleanup_error = detail
        desktop_lane().block(self, detail)

    def request_close(self):
        self.closing = True

    async def _close(self):
        try:
            if self.session is not None:
                desktop_lane().block(self, "REPL close is awaiting native cleanup.")
                # SDK close retires pending execution/reset itself. Waiting for
                # call_lock here would delay cancellation until execution ends.
                result = await self.session.close()
                if result.get("isError"):
                    detail = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
                    raise RuntimeError(detail or "The engine could not confirm native cleanup.")
            self.cleanup_error = None
            desktop_lane().clear_blocker(self)
        except Exception as error:
            self._block_cleanup(str(error) or type(error).__name__)
            raise RuntimeError(
                "Native cleanup is unconfirmed; REPL input remains blocked: " + self.cleanup_error
            ) from error

    async def close(self):
        self.request_close()
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="cua-repl-cleanup")
        await asyncio.shield(self.close_task)
