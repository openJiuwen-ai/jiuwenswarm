"""One owned MCP connection per child; desktop calls share one serial lane."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
import sys
import weakref

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
    """Keep AnyIO context entry/exit in one owner task, including cancellation."""

    def __init__(self, python=None, *, parameters=None, startup_timeout=180, call_timeout=180):
        self.python = python or sys.executable
        self.parameters = parameters
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.owner = self.client = self.ready = self.stop = None
        self.open_lock = asyncio.Lock()
        self.call_lock = asyncio.Lock()
        self.active = set()
        self.used = False
        self.closing = False
        self.cleanup_error = None

    def _parameters(self):
        if self.parameters is not None:
            return self.parameters
        return StdioServerParameters(
            command=self.python,
            args=["-m", "jiuwenswarm.agents.harness.computer_use.worker"],
            env=dict(os.environ),
        )

    async def open(self):
        async with self.open_lock:
            if self.closing or self.cleanup_error:
                raise RuntimeError("The previous REPL was closed; recreate variables in a fresh delegation.")
            if self.client is not None:
                return
            if self.owner is not None:
                if self.owner.done():
                    raise RuntimeError("The REPL connection ended; no code was replayed.")
            else:
                self.ready = asyncio.get_running_loop().create_future()
                self.stop = asyncio.Event()
                self.owner = asyncio.create_task(self._serve(), name="cua-repl-transport")
            try:
                await asyncio.wait_for(asyncio.shield(self.ready), self.startup_timeout)
            except BaseException:
                await asyncio.shield(self.close())
                if self.ready.done() and not self.ready.cancelled():
                    self.ready.exception()
                raise

    async def _serve(self):
        cleanup_confirmed = True
        try:
            async with stdio_client(self._parameters()) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.call_timeout)) as client:
                    await client.initialize()
                    listing = await client.list_tools()
                    if not {"computer_use", "computer_use_reset"}.issubset(t.name for t in listing.tools):
                        raise RuntimeError("The bundled MCP does not provide its execution and cleanup tools.")
                    self.client = client
                    self.ready.set_result(None)
                    try:
                        await self.stop.wait()
                    finally:
                        for task in tuple(self.active):
                            task.cancel()
                        await asyncio.gather(*tuple(self.active), return_exceptions=True)
                        if self.used:
                            cleanup_confirmed = False
                            result = await asyncio.wait_for(client.call_tool("computer_use_reset", {}), 20)
                            if result.isError:
                                detail = "\n".join(block.text for block in result.content if block.type == "text")
                                raise RuntimeError(detail or "The engine could not confirm native cleanup.")
                            cleanup_confirmed = True
        except BaseException as error:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(error)
            elif not cleanup_confirmed:
                self.cleanup_error = str(error) or type(error).__name__
        finally:
            self.client = None

    async def execute(self, code, title=""):
        await self.open()
        async with self.call_lock:
            if self.client is None or self.closing:
                raise RuntimeError("REPL connection ended; earlier actions may have executed. Observe before continuing.")
            self.used = True
            # No retained-history capability is advertised: engine reads stay full.
            arguments = {"code": code}
            if title.strip():
                arguments["title"] = title.strip()
            task = asyncio.create_task(self.client.call_tool("computer_use", arguments))
            self.active.add(task)
            try:
                return await task
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # Reset before the desktop lane admits any subsequent input.
                await asyncio.shield(self.close())
                raise
            finally:
                self.active.discard(task)

    def request_close(self):
        self.closing = True
        if self.stop is not None:
            self.stop.set()
        if self.owner is not None and self.ready is not None and not self.ready.done():
            self.owner.cancel()

    async def close(self):
        self.request_close()
        if self.owner is not None:
            await asyncio.shield(self.owner)
        if self.cleanup_error:
            desktop_lane().blocker = self.cleanup_error
            raise RuntimeError("Native cleanup is unconfirmed; desktop input remains blocked: " + self.cleanup_error)
