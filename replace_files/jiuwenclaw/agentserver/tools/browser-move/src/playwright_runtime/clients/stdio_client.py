# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
from contextlib import AsyncExitStack
from typing import Any, List, Optional

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpToolCard
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT
from openjiuwen.core.foundation.tool.mcp.client.stdio_client import StdioClient


class BrowserMoveStdioClient(StdioClient):
    """browser-move extension of StdioClient.

    Adds timeout resolution, retryable-error detection, auto-reconnect, and
    retry/timeout wrapping around list_tools and call_tool.  Also fixes the
    missing exit-stack reset in connect/disconnect that would otherwise cause
    failures on the second connection attempt.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_timeout(self, timeout: float = NO_TIMEOUT, *, default_s: float = 60.0) -> float:
        """Return an effective timeout in seconds for MCP operations."""
        try:
            configured = float(self._params.get("timeout_s", default_s))
            if configured <= 0:
                configured = default_s
        except (TypeError, ValueError):
            configured = default_s

        if timeout == NO_TIMEOUT:
            return configured

        try:
            parsed = float(timeout)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
        return configured

    @staticmethod
    def _is_retryable_transport_error(error: Exception) -> bool:
        name = type(error).__name__.lower()
        text = str(error).lower()
        markers = (
            "closedresourceerror",
            "brokenresourceerror",
            "endofstream",
            "stream closed",
            "connection closed",
            "remoteprotocolerror",
            "readerror",
            "writeerror",
            "not connected",
            "broken pipe",
        )
        return any(marker in name or marker in text for marker in markers)

    async def _reconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        await self.disconnect(timeout=timeout)
        return await self.connect(timeout=timeout)

    # ------------------------------------------------------------------
    # Overrides — include exit-stack reset fix missing from base class
    # ------------------------------------------------------------------

    async def connect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Establish Stdio connection to the tool server."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        connect_timeout = self._resolve_timeout(timeout, default_s=30.0)
        try:
            valid_handlers = {"strict", "ignore", "replace"}
            handler = self._params.get("encoding_error_handler", "strict")
            if handler not in valid_handlers:
                handler = "strict"
            params = StdioServerParameters(
                command=self._params.get("command"),
                args=self._params.get("args"),
                env=self._params.get("env"),
                cwd=self._params.get("cwd"),
                encoding_error_handler=handler,
            )
            self._exit_stack = AsyncExitStack()
            self._client = stdio_client(params)
            self._read, self._write = await self._exit_stack.enter_async_context(self._client)
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(self._read, self._write, sampling_callback=None)
            )
            await asyncio.wait_for(self._session.initialize(), timeout=connect_timeout)
            self._is_disconnected = False
            logger.info("Stdio client connected successfully")
            return True
        except asyncio.TimeoutError:
            logger.error(f"Stdio connection timed out after {connect_timeout:.1f}s")
            await self.disconnect()
            return False
        except Exception as e:
            logger.error(f"Stdio connection failed: {e}")
            await self.disconnect()
            return False

    async def disconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Close Stdio connection."""
        if self._is_disconnected:
            logger.info("Stdio client disconnected successfully")
            return True
        try:
            await self._exit_stack.aclose()
            logger.info("Stdio client disconnected successfully")
            self._is_disconnected = True
            return True
        except (asyncio.CancelledError, RuntimeError):
            if self._client:
                await self._client.__aexit__(None, None, None)
            logger.info("Stdio client disconnected successfully")
            self._is_disconnected = True
            return True
        except Exception as e:
            logger.error(f"Stdio disconnection failed: {e}")
            return False
        finally:
            self._session = None
            self._client = None
            self._read = None
            self._write = None
            self._exit_stack = AsyncExitStack()

    async def list_tools(self, *, timeout: float = NO_TIMEOUT) -> List[Any]:
        """List available tools via Stdio, with auto-reconnect and timeout."""
        if not self._session:
            connected = await self._reconnect(timeout=timeout)
            if not connected:
                raise RuntimeError("Not connected to Stdio server")

        effective_timeout = self._resolve_timeout(timeout)
        for attempt in range(2):
            try:
                tools_response = await asyncio.wait_for(
                    self._session.list_tools(),
                    timeout=effective_timeout,
                )
                tools_list = [
                    McpToolCard(
                        name=tool.name,
                        server_name=self._name,
                        description=getattr(tool, "description", ""),
                        input_params=getattr(tool, "inputSchema", {}),
                    )
                    for tool in tools_response.tools
                ]
                logger.info(f"Retrieved {len(tools_list)} tools from Stdio server")
                return tools_list
            except asyncio.TimeoutError as e:
                if attempt == 0:
                    logger.warning(
                        f"Stdio list_tools timed out after {effective_timeout:.1f}s, retrying after reconnect"
                    )
                    connected = await self._reconnect(timeout=effective_timeout)
                    if connected:
                        continue
                logger.error(f"Stdio list_tools timed out after {effective_timeout:.1f}s")
                raise RuntimeError(
                    f"Stdio list_tools timed out after {effective_timeout:.1f}s"
                ) from e
            except Exception as e:
                if attempt == 0 and self._is_retryable_transport_error(e):
                    logger.warning(
                        f"Stdio list_tools retry after reconnect: type={type(e).__name__}, repr={e!r}"
                    )
                    connected = await self._reconnect(timeout=timeout)
                    if connected:
                        continue
                logger.error(f"Failed to list tools via Stdio: {e}")
                raise

    async def call_tool(self, tool_name: str, arguments: dict, *, timeout: float = NO_TIMEOUT) -> Any:
        """Call tool via Stdio, with auto-reconnect, timeout, and multi-content extraction."""
        if not self._session:
            connected = await self._reconnect(timeout=timeout)
            if not connected:
                raise RuntimeError("Not connected to Stdio server")

        effective_timeout = self._resolve_timeout(timeout)
        for attempt in range(2):
            try:
                logger.info(f"Calling tool '{tool_name}' via Stdio with arguments: {arguments}")
                tool_result = await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments=arguments),
                    timeout=effective_timeout,
                )
                result_content = None
                if tool_result.content and len(tool_result.content) > 0:
                    chunks = []
                    for item in tool_result.content:
                        text = getattr(item, "text", None)
                        if text:
                            chunks.append(text)
                            continue

                        uri = getattr(item, "uri", None)
                        if uri:
                            chunks.append(str(uri))
                            continue

                        data = getattr(item, "data", None)
                        if data is not None:
                            mime = (
                                getattr(item, "mimeType", None)
                                or getattr(item, "mime_type", None)
                                or "application/octet-stream"
                            )
                            chunks.append(f"[binary content: {mime}]")
                            continue

                        chunks.append(str(item))

                    if chunks:
                        result_content = "\n".join(chunks)
                logger.info(f"Tool '{tool_name}' call completed via Stdio")
                return result_content
            except asyncio.TimeoutError as e:
                if attempt == 0:
                    logger.warning(
                        f"Stdio tool call '{tool_name}' timed out after {effective_timeout:.1f}s, retrying after reconnect"
                    )
                    connected = await self._reconnect(timeout=effective_timeout)
                    if connected:
                        continue
                logger.error(
                    f"Tool call timed out via Stdio: tool='{tool_name}', timeout={effective_timeout:.1f}s"
                )
                raise RuntimeError(
                    f"Stdio tool call timed out for '{tool_name}' after {effective_timeout:.1f}s"
                ) from e
            except Exception as e:
                if attempt == 0 and self._is_retryable_transport_error(e):
                    logger.warning(
                        f"Stdio tool call '{tool_name}' retry after reconnect: type={type(e).__name__}, repr={e!r}"
                    )
                    connected = await self._reconnect(timeout=timeout)
                    if connected:
                        continue
                logger.error(
                    f"Tool call failed via Stdio: type={type(e).__name__}, repr={e!r}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Stdio tool call failed for '{tool_name}': {type(e).__name__}: {e!r}"
                ) from e

    async def get_tool_info(self, tool_name: str, *, timeout: float = NO_TIMEOUT) -> Optional[Any]:
        """Get specific tool info via Stdio."""
        tools = await self.list_tools(timeout=timeout)
        for tool in tools:
            if tool.name == tool_name:
                logger.debug(f"Found tool info for '{tool_name}' via Stdio")
                return tool
        logger.warning(f"Tool '{tool_name}' not found via Stdio")
        return None
