# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import time
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, List, Optional

from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging import runner_logger as logger
from openjiuwen.core.session.tracer import decorate_tool_with_trace
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool import McpToolCard, MCPTool, McpServerConfig
from openjiuwen.core.foundation.tool import (
    McpClient,
    SseClient,
    StdioClient,
    StreamableHttpClient,
    PlaywrightClient
)


@dataclass
class McpServerResource:
    config: McpServerConfig
    client: McpClient
    tool_ids: list[str]
    last_update_time: Any
    expiry_time: Optional[float] = None


@dataclass
class SysOpToolResource:
    sys_op_id: str
    tool_ids: list[str]
    last_update_time: Any


class ToolMgr:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._mcp_server_name_to_ids: dict[str, list[str]] = {}
        self._mcp_server_resources: dict[str, McpServerResource] = {}
        self._sys_op_resources: dict[str, SysOpToolResource] = {}

    def add_tool(self, tool_id: str, tool: Tool) -> None:
        if self._tools.get(tool_id) is not None:
            raise ValueError(f"already exist tool {tool_id}")
        self._tools[tool_id] = tool

    def get_tool(self, tool_id: str, session=None) -> Optional[Tool]:
        tool = self._tools.get(tool_id)
        return decorate_tool_with_trace(tool, session)

    def get_mcp_tool(self, tool_name: str, server_id: str, session):
        resource = self._mcp_server_resources.get(server_id)
        if resource:
            tool_id = self.generate_mcp_tool_id(server_id, resource.config.server_name, tool_name)
            return self.get_tool(tool_id, session)
        return None

    def get_mcp_tools(self, server_id: str, session):
        resource = self._mcp_server_resources.get(server_id)
        if resource:
            tool_ids = resource.tool_ids
            results = []
            for tool_id in tool_ids:
                results.append(self.get_tool(tool_id, session))
            return results
        return None

    def get_mcp_tool_id(self, server_id: str, tool_name=None):
        resource = self._mcp_server_resources.get(server_id)
        if resource:
            if tool_name is None:
                return resource.tool_ids
            tool_id = self.generate_mcp_tool_id(server_id, resource.config.server_name, tool_name)
            return tool_id
        else:
            return None

    def remove_tool(self, tool_id: str) -> Optional[Tool]:
        return self._tools.pop(tool_id, None)

    @staticmethod
    def generate_mcp_tool_id(server_id, server_name, tool_name):
        return f'{server_id}.{server_name}.{tool_name}'

    async def add_tool_server(self, server_config: McpServerConfig, expiry_time: Optional[float] = None) -> List[
        McpToolCard]:
        if self._mcp_server_resources.get(server_config.server_id) is not None:
            raise build_error(StatusCode.RESOURCE_MCP_SERVER_ADD_ERROR, server_config=server_config,
                              reason="server_id is already exist")
        client = self._create_client(server_config)
        # 持续尝试连接：try in every 5s, and max wait time is 120s.
        retry_interval = 5.0
        max_wait_time = 120.0
        start_time = asyncio.get_event_loop().time()
        attempt = 0
        last_error = None
        while True:
            attempt += 1
            elapsed_time = asyncio.get_event_loop().time() - start_time
            # check overtime
            if elapsed_time >= max_wait_time:
                error_msg = (f"MCP server connection timeout after {elapsed_time:.1f}s "
                        f"(max_wait_time={max_wait_time}s), attempted {attempt} times. "
                        f"Last error: {last_error}")
                logger.error(error_msg)
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_CONNECTION_ERROR,
                                server_config=server_config,
                                reason=error_msg)
            try:
                logger.info(f"Attempting to connect to MCP server (attempt {attempt}, "
                        f"elapsed={elapsed_time:.1f}s/{max_wait_time}s), "
                        f"server_id={server_config.server_id}, server_name={server_config.server_name}")
                connected = await client.connect()
                if not connected:
                    last_error = f"connect() returned False on attempt {attempt}"
                    logger.warning(f"MCP server connection failed (attempt {attempt}): "
                                f"connect() returned False, server_id={server_config.server_id}")
                    remaining_time = max_wait_time - elapsed_time
                    if remaining_time > retry_interval:
                        logger.info(f"Retrying in {retry_interval} seconds... (remaining time: {remaining_time:.1f}s)")
                        await asyncio.sleep(retry_interval)
                        continue
                    elif remaining_time > 0:
                        logger.info(f"Final attempt: waiting {remaining_time:.1f}s before last try...")
                        await asyncio.sleep(remaining_time)
                        continue
                    else:
                        # overtime
                        continue
                # fresh tool list
                logger.info(f"MCP server connected successfully after {elapsed_time:.1f}s "
                        f"(attempt {attempt}), server_id={server_config.server_id}")
                results = await self._inner_refresh_mcp_tools(client, server_config, expiry_time)
                self._mcp_server_name_to_ids.setdefault(server_config.server_name, []).append(server_config.server_id)
                return results
            except Exception as e:
                last_error = str(e)
                logger.error(f"MCP server connection error (attempt {attempt}, elapsed={elapsed_time:.1f}s): "
                            f"server_id={server_config.server_id}, error={str(e)}")
                error_msg = (f"MCP server connection failed with exception after {elapsed_time:.1f}s "
                        f"(attempt {attempt}): {last_error}")
                logger.error(error_msg)
                # pylint: disable=raise-missing-from
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_ADD_ERROR, cause=e,
                                server_config=server_config,
                                reason=error_msg) from e

    @staticmethod
    def _create_client(config: McpServerConfig) -> McpClient:
        if config.client_type == "sse":
            return SseClient(
                config.server_path,
                config.server_name,
                config.auth_headers,
                config.auth_query_params,
            )
        elif config.client_type in {"streamable-http", "streamable_http", "http"}:
            return StreamableHttpClient(config)
        elif config.client_type == "stdio":
            return StdioClient(config)
        elif config.client_type == "playwright":
            return PlaywrightClient(config)
        elif config.client_type == "openapi":
            from openjiuwen.core.foundation.tool.mcp.client import OpenApiClient
            return OpenApiClient(config.server_path, config.server_name)
        else:
            raise ValueError(f"Unsupported MCP client type: {config.client_type}")

    def get_mcp_server_ids(self, server_name: str):
        return self._mcp_server_name_to_ids.get(server_name, [])

    async def remove_tool_server(self, server_id: str, ignore_not_exist: bool = True) -> list[str]:
        mcp_server_resource = self._mcp_server_resources.pop(server_id, None)
        if not mcp_server_resource:
            if not ignore_not_exist:
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_REMOVE_ERROR, server_id=server_id,
                                  reason="server is not exist")
            else:
                return []
        try:
            await mcp_server_resource.client.disconnect()
        except Exception as e:
            logger.warn(f"remove tool server discount {str(e)}, server_id={server_id}")
        finally:
            self._inner_remove_mcp_tools(mcp_server_resource.tool_ids)
            ids = self._mcp_server_name_to_ids.get(mcp_server_resource.config.server_name)
            if ids and server_id in ids:
                ids.remove(server_id)
            if not ids:
                self._mcp_server_name_to_ids.pop(mcp_server_resource.config.server_name)
        return mcp_server_resource.tool_ids

    def add_sys_operation_tools(self, sys_op_id: str, tool_ids: list[str]) -> None:
        """Register tools associated with a SysOperation."""
        if not tool_ids:
            return
        self._sys_op_resources[sys_op_id] = SysOpToolResource(
            sys_op_id=sys_op_id,
            tool_ids=deepcopy(tool_ids),
            last_update_time=time.time()
        )

    def remove_sys_operation_tools(self, sys_op_id: str) -> list[str]:
        """Unregister and return tool IDs associated with a SysOperation."""
        resource = self._sys_op_resources.pop(sys_op_id, None)
        return resource.tool_ids if resource else []

    async def refresh_tool_server(self, server_id: str, skip_not_exist: bool = False, force: bool = False) -> list[
        McpToolCard]:
        mcp_resource = self._mcp_server_resources.get(server_id)
        if not mcp_resource:
            if not skip_not_exist:
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_REFRESH_ERROR, server_id=server_id,
                                  reason="server is not exist")
            return []
        need_refresh = force
        if not force:
            if mcp_resource.expiry_time and time.time() - mcp_resource.last_update_time >= mcp_resource.expiry_time:
                need_refresh = True

        if need_refresh:
            return await self._inner_refresh_mcp_tools(mcp_resource.client, mcp_resource.config,
                                                       mcp_resource.expiry_time)
        else:
            return []

    async def _inner_refresh_mcp_tools(self, client, server_config, expiry_time):
        mcp_cards = await client.list_tools()
        mcp_cards = mcp_cards if mcp_cards else []
        for card in mcp_cards:
            card.id = self.generate_mcp_tool_id(server_config.server_id, server_config.server_name, card.name)
            self.add_tool(card.id, MCPTool(mcp_client=client, tool_info=deepcopy(card)))
        mcp_ids = [card.id for card in mcp_cards]
        self._mcp_server_resources[server_config.server_id] = McpServerResource(config=server_config,
                                                                                expiry_time=expiry_time,
                                                                                client=client,
                                                                                last_update_time=time.time(),
                                                                                tool_ids=deepcopy(mcp_ids))
        return mcp_cards

    def _inner_remove_mcp_tools(self, tools):
        if not tools:
            return
        for tool_id in tools:
            try:
                self.remove_tool(tool_id)
            except Exception:
                continue

    async def release(self):
        for res in self._mcp_server_resources.values():
            try:
                await res.client.disconnect()
            except Exception:
                continue
