# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import asyncio
from contextlib import AsyncExitStack
from typing import Any, List, Optional, Dict

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpToolCard
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT
from openjiuwen.core.foundation.tool.mcp.client.mcp_client import McpClient


class StdioClient(McpClient):
    """Stdio transport based MCP client"""

    def __init__(self, server_path: str, name: str, params: Dict = None):
        super().__init__(server_path)
        self._name = name
        self._client = None
        self._session = None
        self._read = None
        self._write = None
        self._params = params if params else {}
        self._exit_stack = AsyncExitStack()
        self._is_disconnected: bool = False
        self._owner_task: Optional[asyncio.Task] = None
        self._owner_ready: asyncio.Event = asyncio.Event()
        self._owner_close: asyncio.Event = asyncio.Event()
        self._connect_result: Optional[bool] = None
        self._connect_exception: Optional[Exception] = None
        # Caller-provided connect timeout, propagated to _run_owner for the
        # actual _session.initialize() call so short timeouts are honored.
        self._connect_timeout: float = NO_TIMEOUT
        # Strong reference to an owner task that did not finish within
        # _force_close's cancel grace period, kept so the task object and
        # its exit stack are not GC'd while aclose() is still pending.
        self._leaked_owner_task: Optional[asyncio.Task] = None

    async def _run_owner(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            try:
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
                self._exit_stack = AsyncExitStack()
                self._client = stdio_client(params)
                self._read, self._write = await self._exit_stack.enter_async_context(self._client)
                self._session = await self._exit_stack.enter_async_context(
                    ClientSession(self._read, self._write, sampling_callback=None))
                connect_timeout = self._connect_timeout if self._connect_timeout != NO_TIMEOUT else 30.0
                await asyncio.wait_for(self._session.initialize(), timeout=connect_timeout)
                self._connect_result = True
                self._is_disconnected = False
                logger.info("Stdio client connected successfully")
            except asyncio.TimeoutError as e:
                self._connect_result = False
                self._connect_exception = e
                logger.error(f"Stdio connection timed out: {e}")
            except Exception as e:
                self._connect_result = False
                self._connect_exception = e
                logger.error(f"Stdio connection failed: {e}")
            finally:
                self._owner_ready.set()

            try:
                await asyncio.wait_for(self._owner_close.wait(), timeout=3600.0)
            except asyncio.TimeoutError:
                logger.warning("Stdio client owner task timeout waiting for close signal")
            except asyncio.CancelledError:
                logger.info("Stdio client owner task cancelled")
            except Exception as e:
                logger.error(f"Stdio client owner task wait error: {e}")
        finally:
            try:
                await self._exit_stack.aclose()
                logger.info("Stdio client disconnected successfully")
                self._is_disconnected = True
            except Exception as e:
                logger.error(f"Stdio disconnection failed: {e}")
                self._is_disconnected = True
            finally:
                self._owner_task = None
                self._session = None
                self._client = None
                self._read = None
                self._write = None
                self._exit_stack = AsyncExitStack()
                # Owner task completed its own aclose() — no leak for this run.
                self._leaked_owner_task = None

    async def connect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Establish Stdio connection to the tool server"""
        if self._owner_task is not None:
            logger.warning("Stdio client already connecting or connected")
            return False

        self._connect_result = None
        self._connect_exception = None
        self._connect_timeout = timeout
        self._owner_ready.clear()
        self._owner_close.clear()

        self._owner_task = asyncio.create_task(self._run_owner())

        try:
            if timeout == NO_TIMEOUT:
                await self._owner_ready.wait()
            else:
                await asyncio.wait_for(self._owner_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Stdio connection timeout")
            await self._force_close()
            return False

        if self._connect_exception is not None:
            await self._force_close()
            return False

        return self._connect_result is True

    async def disconnect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        """Close SSE connection"""
        if self._is_disconnected:
            logger.info("Stdio client already disconnected")
            return True
        if self._owner_task is None:
            logger.info("Stdio client not connected")
            return True

        self._owner_close.set()

        try:
            if timeout == NO_TIMEOUT:
                await self._owner_task
            else:
                await asyncio.wait_for(self._owner_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Stdio disconnect timeout")
            await self._force_close()
            return False
        except asyncio.CancelledError:
            logger.error(f"Stdio disconnect cancelled")
            await self._force_close()
            raise
        except Exception as e:
            logger.error(f"Stdio disconnect exception: {e}")
            return False

        # _run_owner's finally block sets _owner_task = None, but set it
        # here as well for explicitness and robustness against race conditions.
        self._owner_task = None
        return self._is_disconnected

    async def _force_close(self):
        leaked_owner_task: Optional[asyncio.Task] = None
        if self._owner_task and not self._owner_task.done():
            self._owner_close.set()
            try:
                await asyncio.wait_for(self._owner_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._owner_task.cancel()
                try:
                    await asyncio.wait_for(self._owner_task, timeout=2.0)
                except asyncio.CancelledError:
                    logger.warning(
                        "Stdio client owner task was cancelled during graceful close"
                    )
                except Exception as e:
                    logger.warning(
                        "Stdio client owner task close raised exception: %r", e
                    )
                # Owner task did not finish within cancel grace period — it may
                # still be stuck inside _exit_stack.aclose() (e.g., subprocess
                # stdin closure blocked). Keep a strong reference so the task
                # object is not GC'd while we clear our handle, and surface a
                # warning so operators can detect subprocess pipe leaks.
                if not self._owner_task.done():
                    leaked_owner_task = self._owner_task
                    logger.warning(
                        "Stdio client owner task did not terminate after cancel; "
                        "subprocess pipe may leak. task=%r",
                        leaked_owner_task,
                    )
            except (asyncio.CancelledError, Exception):
                pass
        self._owner_task = None
        self._session = None
        self._client = None
        self._read = None
        self._write = None
        # Reset the exit stack so a subsequent connect() starts fresh; the
        # leaked owner task (if any) still holds the old stack and will run
        # its own aclose() independently.
        self._exit_stack = AsyncExitStack()
        self._leaked_owner_task = leaked_owner_task
        self._is_disconnected = True

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