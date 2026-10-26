# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import time
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
        connected = False
        try:
            connected = await client.connect()
            if not connected:
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_CONNECTION_ERROR, server_config=server_config,
                                  reason="")
            results = await self._inner_refresh_mcp_tools(client, server_config, expiry_time)
            self._mcp_server_name_to_ids.setdefault(server_config.server_name, []).append(server_config.server_id)
            return results
        except Exception as e:
            if connected:
                try:
                    await client.disconnect()
                except Exception as disconnect_exc:
                    logger.error(
                        f"add_tool_server cleanup failed: {disconnect_exc}, "
                        f"server_id={server_config.server_id}"
                    )
            raise build_error(StatusCode.RESOURCE_MCP_SERVER_ADD_ERROR, cause=e, server_config=server_config,
                              reason=str(e))

    @staticmethod
    def _create_client(config: McpServerConfig) -> McpClient:
        if config.client_type == "sse":
            return SseClient(config.server_path, config.server_name,
                             config.auth_headers, config.auth_query_params)
        elif config.client_type in {"streamable-http", "streamable_http", "http"}:
            return StreamableHttpClient(
                config.server_path,
                config.server_name,
                config.auth_headers,
                config.auth_query_params,
            )
        elif config.client_type == "stdio":
            return StdioClient(config.server_path, config.server_name, config.params)
        elif config.client_type == "playwright":
            return PlaywrightClient(config.server_path, config.server_name)
        elif config.client_type == "openapi":
            from openjiuwen.core.foundation.tool.mcp.client import OpenApiClient
            return OpenApiClient(config.server_path, config.server_name)
        else:
            raise ValueError(f"Unsupported MCP client type: {config.client_type}")

    def get_mcp_server_ids(self, server_name: str):
        return self._mcp_server_name_to_ids.get(server_name, [])

    async def remove_tool_server(self, server_id: str, ignore_not_exist: bool = True) -> list[str]:
        mcp_server_resource = self._mcp_server_resources.get(server_id)
        if not mcp_server_resource:
            if not ignore_not_exist:
                raise build_error(StatusCode.RESOURCE_MCP_SERVER_REMOVE_ERROR, server_id=server_id,
                                  reason="server is not exist")
            else:
                return []

        try:
            success = await mcp_server_resource.client.disconnect()
            if not success:
                logger.error(f"remove tool server disconnect failed, server_id={server_id}")
                return []
        except Exception as e:
            logger.error(f"remove tool server disconnect exception: {e}, server_id={server_id}")
            return []

        self._mcp_server_resources.pop(server_id, None)

        try:
            self._inner_remove_mcp_tools(mcp_server_resource.tool_ids)
            ids = self._mcp_server_name_to_ids.get(mcp_server_resource.config.server_name)
            if ids and server_id in ids:
                ids.remove(server_id)
            if not ids:
                self._mcp_server_name_to_ids.pop(mcp_server_resource.config.server_name)
        except Exception as e:
            logger.error(f"remove tool server cleanup failed: {e}, server_id={server_id}")

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
            except Exception as e:
                logger.warning(f"Failed to remove MCP tool {tool_id}: {e}")
                continue

    async def release(self):
        for res in self._mcp_server_resources.values():
            try:
                await res.client.disconnect()
            except Exception as e:
                logger.warning(f"Failed to disconnect MCP client during release: {e}")
                continue
