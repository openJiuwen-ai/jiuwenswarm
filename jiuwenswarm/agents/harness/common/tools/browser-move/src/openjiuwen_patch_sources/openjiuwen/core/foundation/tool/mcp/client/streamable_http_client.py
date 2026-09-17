# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import asyncio
from contextlib import AsyncExitStack
from typing import Any, List, Optional, Dict, AsyncGenerator

import httpx

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpToolCard
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT
from openjiuwen.core.foundation.tool.mcp.client.mcp_client import McpClient


class StreamableHttpClient(McpClient):
    """Streamable HTTP transport based MCP client."""

    def __init__(
        self,
        server_path: str,
        name: str,
        auth_headers: Optional[Dict[str, str]] = None,
        auth_query_params: Optional[Dict[str, str]] = None,
    ):
        super().__init__(server_path)
        self._name = name
        self._client = None
        self._session = None
        self._read = None
        self._write = None
        self._get_session_id = None
        self._exit_stack = AsyncExitStack()
        self._is_disconnected: bool = False
        self._reconnect_lock = asyncio.Lock()
        if auth_headers is not None or auth_query_params is not None:
            self._auth_provider = AuthHeaderAndQueryProvider(
                auth_headers=auth_headers or {},
                auth_query_params=auth_query_params or {},
            )
            logger.info("Using custom header and query authorization for Streamable HTTP client")
        else:
            self._auth_provider = None

    @staticmethod
    def _is_retryable_transport_error(error: Exception) -> bool:
        name = error.__class__.__name__.lower()
        text = str(error).lower()
        markers = (
            "session terminated",
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
        return any(m in name or m in text for m in markers)

    async def _reconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        async with self._reconnect_lock:
            return await self.connect(retry_times=1, timeout=timeout)

    async def connect(self, *, timeout: float = NO_TIMEOUT, retry_times: int = 1) -> bool:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        actual_timeout = timeout if timeout != NO_TIMEOUT else 60.0
        attempts = max(1, int(retry_times))
        for attempt in range(1, attempts + 1):
            try:
                await self.disconnect(timeout=timeout)
                self._client = streamablehttp_client(
                    self._server_path,
                    timeout=actual_timeout,
                    auth=self._auth_provider,
                )
                self._read, self._write, self._get_session_id = await self._exit_stack.enter_async_context(self._client)
                self._session = await self._exit_stack.enter_async_context(
                    ClientSession(self._read, self._write, sampling_callback=None)
                )
                await asyncio.wait_for(self._session.initialize(), timeout=actual_timeout)
                self._is_disconnected = False
                logger.info(f"Streamable HTTP client connected successfully to {self._server_path}")
                return True
            except asyncio.TimeoutError:
                logger.error(
                    f"Streamable HTTP connection timed out after {actual_timeout:.1f}s "
                    f"(attempt {attempt}/{attempts}): {self._server_path}"
                )
                await self.disconnect(timeout=timeout)
            except Exception as e:
                logger.error(
                    f"Streamable HTTP connection failed to {self._server_path} "
                    f"(attempt {attempt}/{attempts}): {e}"
                )
                await self.disconnect(timeout=timeout)
        return False

    async def disconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Close Streamable HTTP connection."""
        try:
            await self._exit_stack.aclose()
            logger.info("Streamable HTTP client disconnected successfully")
            self._is_disconnected = True
            return True
        except Exception as e:
            logger.error(f"Streamable HTTP disconnection failed: {e}")
            return False
        finally:
            self._session = None
            self._client = None
            self._read = None
            self._write = None
            self._get_session_id = None
            self._exit_stack = AsyncExitStack()

    async def list_tools(self, *, timeout: float = NO_TIMEOUT) -> List[Any]:
        """List available tools via Streamable HTTP."""
        if not self._session:
            connected = await self._reconnect(timeout=timeout)
            if not connected:
                raise RuntimeError("Not connected to Streamable HTTP server")

        for attempt in range(2):
            try:
                tools_response = await self._session.list_tools()
                tools_list = [
                    McpToolCard(
                        name=tool.name,
                        server_name=self._name,
                        description=getattr(tool, "description", ""),
                        input_params=getattr(tool, "inputSchema", {}),
                    )
                    for tool in tools_response.tools
                ]
                logger.info(f"Retrieved {len(tools_list)} tools from Streamable HTTP server")
                return tools_list
            except Exception as e:
                if attempt == 0 and self._is_retryable_transport_error(e):
                    logger.warning(f"Streamable HTTP list_tools retry after reconnect: {e}")
                    connected = await self._reconnect(timeout=timeout)
                    if connected:
                        continue
                logger.error(f"Failed to list tools via Streamable HTTP: {e}")
                raise

    async def call_tool(self, tool_name: str, arguments: dict, *, timeout: float = NO_TIMEOUT) -> Any:
        """Call tool via Streamable HTTP."""
        if not self._session:
            connected = await self._reconnect(timeout=timeout)
            if not connected:
                raise RuntimeError("Not connected to Streamable HTTP server")

        for attempt in range(2):
            try:
                logger.info(f"Calling tool '{tool_name}' via Streamable HTTP with arguments: {arguments}")
                tool_result = await self._session.call_tool(tool_name, arguments=arguments)
                result_content = None
                if tool_result.content and len(tool_result.content) > 0:
                    last_item = tool_result.content[-1]
                    if hasattr(last_item, "text"):
                        result_content = last_item.text
                    elif hasattr(last_item, "data"):
                        mime = getattr(last_item, "mimeType", "image/png")
                        result_content = f"data:{mime};base64,{last_item.data}"
                logger.info(f"Tool '{tool_name}' call completed via Streamable HTTP")
                return result_content
            except Exception as e:
                if attempt == 0 and self._is_retryable_transport_error(e):
                    logger.warning(f"Streamable HTTP tool call '{tool_name}' retry after reconnect: {e}")
                    connected = await self._reconnect(timeout=timeout)
                    if connected:
                        continue
                logger.error(f"Tool call failed via Streamable HTTP: {e}")
                raise

    async def get_tool_info(self, tool_name: str, *, timeout: float = NO_TIMEOUT) -> Optional[Any]:
        """Get specific tool info via Streamable HTTP."""
        tools = await self.list_tools(timeout=timeout)
        for tool in tools:
            if tool.name == tool_name:
                logger.debug(f"Found tool info for '{tool_name}' via Streamable HTTP")
                return tool
        logger.warning(f"Tool '{tool_name}' not found via Streamable HTTP")
        return None


class AuthHeaderAndQueryProvider(httpx.Auth):
    def __init__(self, auth_headers: Dict[str, str], auth_query_params: Dict[str, str]):
        self.headers = auth_headers
        self.query_params = auth_query_params

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.headers:
            for key, value in self.headers.items():
                request.headers[key] = value

        if self.query_params:
            url = request.url.copy_merge_params(self.query_params)
            request.url = url

        yield request
