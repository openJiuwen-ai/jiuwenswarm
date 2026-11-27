# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import asyncio
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpServerConfig, McpToolCard
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT
from openjiuwen.core.foundation.tool.mcp.client.mcp_client import McpClient


class StdioClient(McpClient):
    """Stdio transport based MCP client"""

    __client_name__ = "stdio"
    __client_type__ = "mcp"

    def __init__(
        self,
        config: McpServerConfig | str,
        name: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        resolved_config = self._normalize_config(config, name=name, params=params)
        super().__init__(resolved_config)
        self._name = resolved_config.server_name
        self._client = None
        self._session = None
        self._read = None
        self._write = None
        self._params = resolved_config.params if resolved_config.params else {}
        self._exit_stack = AsyncExitStack()
        self._is_disconnected: bool = False

    @staticmethod
    def _normalize_config(
        config: McpServerConfig | str,
        *,
        name: Optional[str],
        params: Optional[Dict[str, Any]],
    ) -> McpServerConfig:
        if isinstance(config, McpServerConfig):
            return config
        resolved_name = (name or "").strip() or "stdio"
        return McpServerConfig(
            server_id=resolved_name,
            server_name=resolved_name,
            server_path=config,
            client_type="stdio",
            params=params or {},
        )

    async def connect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Establish Stdio connection to the tool server"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        
        try:
            # server_path should be StdioServerParameters for stdio client
            valid_handlers = {"strict", "ignore", "replace"}
            handler = self._params.get('encoding_error_handler', 'strict')
            if handler not in valid_handlers:
                handler = 'strict'
            params = StdioServerParameters(command=self._params.get('command'),
                                           args=self._params.get('args'),
                                           env=self._params.get('env'),
                                           cwd=self._params.get('cwd'),
                                           encoding_error_handler=handler
                                           )

            self._client = stdio_client(params)
            self._read, self._write = await self._exit_stack.enter_async_context(self._client)
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(self._read, self._write, sampling_callback=None))
            await self._session.initialize()
            self._is_disconnected = False
            logger.info("Stdio client connected successfully")
            return True
        except Exception as e:
            logger.error(f"Stdio connection failed: {e}")
            await self.disconnect()
            return False

    async def disconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Close SSE connection"""
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

    async def list_tools(self, *, timeout: float = NO_TIMEOUT) -> List[Any]:
        """List available tools via Stdio"""
        if not self._session:
            raise RuntimeError("Not connected to Stdio server")

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
            logger.info(f"Retrieved {len(tools_list)} tools from Stdio server")
            return tools_list
        except Exception as e:
            logger.error(f"Failed to list tools via Stdio: {e}")
            raise

    async def call_tool(self, tool_name: str, arguments: dict, *, timeout: float = NO_TIMEOUT) -> Any:
        """Call tool via Stdio"""
        if not self._session:
            raise RuntimeError("Not connected to Stdio server")

        try:
            logger.info(f"Calling tool '{tool_name}' via Stdio with arguments: {arguments}")
            tool_result = await self._session.call_tool(tool_name, arguments=arguments)
            result_content = None
            if tool_result.content and len(tool_result.content) > 0:
                last_item = tool_result.content[-1]
                if hasattr(last_item, "text"):
                    result_content = last_item.text
                elif hasattr(last_item, "data"):
                    mime = getattr(last_item, "mimeType", "image/png")
                    result_content = f"data:{mime};base64,{last_item.data}"
            logger.info(f"Tool '{tool_name}' call completed via Stdio")
            return result_content
        except Exception as e:
            logger.error(f"Tool call failed via Stdio: {e!r}")
            raise

    async def get_tool_info(self, tool_name: str, *, timeout: float = NO_TIMEOUT) -> Optional[Any]:
        """Get specific tool info via Stdio"""
        tools = await self.list_tools(timeout=timeout)
        for tool in tools:
            if tool.name == tool_name:
                logger.debug(f"Found tool info for '{tool_name}' via Stdio")
                return tool
        logger.warning(f"Tool '{tool_name}' not found via Stdio")
        return None
