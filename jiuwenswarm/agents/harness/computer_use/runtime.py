"""One owned MCP transport per live desktop subagent; no action replay."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .config import ComputerUseSettings
from .desktop_lease import DesktopLease

logger = logging.getLogger(__name__)


class ComputerUseRuntime:

    def __init__(self, settings: ComputerUseSettings):
        self.settings = settings
        self.id = "computer_use_" + uuid4().hex
        self.platform: str | None = None
        self.tools: dict[str, types.Tool] = {}
        self._client: ClientSession | None = None
        self._owner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None
        self._calls: set[asyncio.Task] = set()
        self._call_lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._lease: DesktopLease | None = None
        self._used = False
        self._closing = False
        self._cleanup_error: Exception | None = None

    async def open(self) -> None:
        async with self._open_lock:
            if self._cleanup_error is not None:
                raise RuntimeError(
                    "Previous desktop cleanup is unconfirmed; no new execution was started.") from self._cleanup_error
            if self._client is not None and not self._closing:
                return
            if self._owner is not None and not self._owner.done():
                raise RuntimeError("Computer Use is closing; wait for cleanup before continuing.")
            self._lease = DesktopLease(self.settings.desktop_id, self.settings.lock_dir)
            await self._lease.acquire()
            self._closing = self._used = False
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._owner = asyncio.create_task(self._serve(), name=self.id)
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), self.settings.startup_timeout)
            except BaseException:
                await asyncio.shield(self.close())
                if self._ready.done() and not self._ready.cancelled():
                    self._ready.exception()  # Consume a late startup failure.
                raise

    async def _serve(self) -> None:
        # Both AnyIO transport contexts are entered and exited by this owner
        # task. Rails, tool calls and cancellation never move its cancel scopes.
        cleanup_confirmed = True
        try:
            params = StdioServerParameters(
                command=self.settings.command,
                args=list(self.settings.args),
                env={**os.environ, **self.settings.env},
                cwd=self.settings.cwd,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.settings.call_timeout),
                ) as client:
                    initialized = await client.initialize()
                    listing = await client.list_tools()
                    self.tools = {tool.name: tool for tool in listing.tools}
                    if not {"computer_use", "computer_use_reset"}.issubset(self.tools):
                        raise RuntimeError("MCP server must provide computer_use and computer_use_reset.")
                    experimental = initialized.capabilities.experimental or {}
                    host = experimental.get("computer-use-host", {})
                    self.platform = (host.get("platform") if host.get("version") == 1 else None)
                    self._client = client
                    self._ready.set_result(None)
                    try:
                        await self._stop.wait()
                    finally:
                        for call in tuple(self._calls):
                            call.cancel()
                        await asyncio.gather(*tuple(self._calls), return_exceptions=True)
                        if self._used:
                            cleanup_confirmed = False
                            result = await asyncio.wait_for(
                                client.call_tool("computer_use_reset", {}),
                                self.settings.cleanup_timeout,
                            )
                            if result.isError:
                                detail = "\n".join(block.text for block in result.content
                                                   if isinstance(block, types.TextContent))
                                raise RuntimeError("Computer Use native cleanup is unconfirmed: " + detail)
                            cleanup_confirmed = True
        except BaseException as error:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(error)
            elif not cleanup_confirmed:
                self._cleanup_error = (error if isinstance(error, Exception) else RuntimeError(str(error)))
                logger.error("Computer Use cleanup failed; desktop lease retained", exc_info=True)
            else:
                logger.debug("Computer Use transport closed", exc_info=True)
        finally:
            self._client = None
            if cleanup_confirmed and self._lease is not None:
                self._lease.release()

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name not in {"computer_use", "computer_use_reset"}:
            raise ValueError("Unsupported Computer Use tool")
        await self.open()
        async with self._call_lock:
            if self._client is None or self._closing:
                raise RuntimeError("Computer Use connection ended. Actions may have executed; "
                                   "observe in a fresh task before continuing.")
            # No history capability/cursor is advertised: the first integration
            # delivers full observations, even after context compaction.
            request = asyncio.create_task(self._client.call_tool(name, arguments=arguments))
            self._calls.add(request)
            self._used = True
            try:
                return await request
            except asyncio.CancelledError:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                raise
            except Exception as error:
                raise RuntimeError(
                    "Computer Use call failed; earlier actions may have executed. No action was retried. " +
                    str(error)) from error
            finally:
                self._calls.discard(request)

    def request_close(self) -> None:
        self._closing = True
        if self._stop is not None:
            self._stop.set()
        if (self._ready is not None and not self._ready.done() and self._owner is not None):
            self._owner.cancel()

    async def close(self) -> None:
        self.request_close()
        if self._owner is not None:
            await asyncio.shield(self._owner)
        if self._cleanup_error is not None:
            raise RuntimeError("Computer Use cleanup failed; this desktop remains reserved until cleanup is resolved."
                               ) from self._cleanup_error
