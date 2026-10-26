# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpToolCard
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT

try:
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import StreamableHttpClient
except ModuleNotFoundError:
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient as StreamableHttpClient

T = TypeVar("T")


class BrowserMoveStreamableHttpClient(StreamableHttpClient):
    """Streamable HTTP MCP client that uses short-lived per-operation sessions."""

    def __init__(
        self,
        server_path: str,
        name: str,
        auth_headers: Optional[Dict[str, str]] = None,
        auth_query_params: Optional[Dict[str, str]] = None,
    ):
        super().__init__(server_path, name, auth_headers, auth_query_params)
        self._io_lock = asyncio.Lock()

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

    async def _with_fresh_session(
        self,
        operation: Callable[[Any], Awaitable[T]],
        *,
        timeout: float = NO_TIMEOUT,
    ) -> T:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        actual_timeout = timeout if timeout != NO_TIMEOUT else 60.0
        client = streamablehttp_client(
            self._server_path,
            timeout=actual_timeout,
            auth=self._auth_provider,
        )
        async with client as (read, write, _get_session_id):
            async with ClientSession(read, write, sampling_callback=None) as session:
                await asyncio.wait_for(session.initialize(), timeout=actual_timeout)
                return await operation(session)

    async def connect(self, *, retry_times: int = 1, timeout: float = NO_TIMEOUT) -> bool:
        attempts = max(1, int(retry_times))
        for attempt in range(1, attempts + 1):
            try:
                async with self._io_lock:
                    await self._with_fresh_session(lambda _session: asyncio.sleep(0), timeout=timeout)
                self._is_disconnected = False
                logger.info(f"Streamable HTTP client connected successfully to {self._server_path}")
                return True
            except asyncio.TimeoutError:
                logger.error(
                    f"Streamable HTTP connection timed out (attempt {attempt}/{attempts}): {self._server_path}"
                )
            except Exception as e:
                logger.error(
                    f"Streamable HTTP connection failed to {self._server_path} "
                    f"(attempt {attempt}/{attempts}): {e}"
                )
        self._is_disconnected = True
        return False

    async def disconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        self._session = None
        self._client = None
        self._read = None
        self._write = None
        self._get_session_id = None
        self._is_disconnected = True
        logger.info("Streamable HTTP client disconnected successfully")
        return True

    async def list_tools(self, *, timeout: float = NO_TIMEOUT) -> List[Any]:
        async def _list(session: Any) -> List[Any]:
            tools_response = await session.list_tools()
            return [
                McpToolCard(
                    name=tool.name,
                    server_name=self._name,
                    description=getattr(tool, "description", ""),
                    input_params=getattr(tool, "inputSchema", {}),
                )
                for tool in tools_response.tools
            ]

        async with self._io_lock:
            for attempt in range(2):
                try:
                    tools_list = await self._with_fresh_session(_list, timeout=timeout)
                    logger.info(f"Retrieved {len(tools_list)} tools from Streamable HTTP server")
                    self._is_disconnected = False
                    return tools_list
                except Exception as e:
                    if attempt == 0 and self._is_retryable_transport_error(e):
                        logger.warning(f"Streamable HTTP list_tools retry with fresh session: {e}")
                        continue
                    logger.error(f"Failed to list tools via Streamable HTTP: {e}")
                    raise

    async def call_tool(self, tool_name: str, arguments: dict, *, timeout: float = NO_TIMEOUT) -> Any:
        async def _call(session: Any) -> Any:
            logger.info(f"Calling tool '{tool_name}' via Streamable HTTP with arguments: {arguments}")
            tool_result = await session.call_tool(tool_name, arguments=arguments)
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
            return result_content

        async with self._io_lock:
            for attempt in range(2):
                try:
                    result_content = await self._with_fresh_session(_call, timeout=timeout)
                    logger.info(f"Tool '{tool_name}' call completed via Streamable HTTP")
                    self._is_disconnected = False
                    return result_content
                except Exception as e:
                    if attempt == 0 and self._is_retryable_transport_error(e):
                        logger.warning(f"Streamable HTTP tool call '{tool_name}' retry with fresh session: {e}")
                        continue
                    logger.error(f"Tool call failed via Streamable HTTP: {e}")
                    raise

    async def get_tool_info(self, tool_name: str, *, timeout: float = NO_TIMEOUT) -> Optional[Any]:
        tools = await self.list_tools(timeout=timeout)
        for tool in tools:
            if tool.name == tool_name:
                logger.debug(f"Found tool info for '{tool_name}' via Streamable HTTP")
                return tool
        logger.warning(f"Tool '{tool_name}' not found via Streamable HTTP")
        return None
