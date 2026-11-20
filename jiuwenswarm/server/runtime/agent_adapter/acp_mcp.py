# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expose tools owned by the connected ACP bridge for a single Runtime turn."""

from contextlib import contextmanager
import re

from openjiuwen.core.foundation.tool import Tool, ToolCard

from jiuwenswarm.acp.session_mcp import MCP_CALL_METHOD, MCP_TOOLS_PARAM
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager


class AcpMcpTool(Tool):
    def __init__(self, card: ToolCard, session_id: str, token: str) -> None:
        super().__init__(card)
        self._session_id = session_id
        self._token = token

    async def invoke(self, inputs: dict, **kwargs) -> dict:
        response = await get_acp_output_manager().send_jsonrpc_request(
            MCP_CALL_METHOD, {"token": self._token, "name": self.card.name, "arguments": inputs},
            session_id=self._session_id, timeout=50,
        )
        if "error" in response:
            raise RuntimeError("ACP session MCP call failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Invalid ACP session MCP result")
        return result

    async def stream(self, inputs: dict, **kwargs):
        yield await self.invoke(inputs, **kwargs)


@contextmanager
def bind_acp_mcp_tools(adapter, request):
    declarations = (request.params or {}).get(MCP_TOOLS_PARAM)
    if request.channel_id != "acp" or not request.session_id or not isinstance(declarations, list):
        yield
        return
    manager = adapter._instance.ability_manager
    names = adapter._collect_registered_ability_names()
    registered = set()
    try:
        for item in declarations:
            if not isinstance(item, dict):
                continue
            name, token = item.get("name"), item.get("token")
            schema, description = item.get("schema"), item.get("description")
            if (
                not isinstance(name, str) or not re.fullmatch(r"mcp__[A-Za-z0-9_-]{1,59}", name)
                or name in names or name in registered or not isinstance(schema, dict)
                or not isinstance(description, str) or not isinstance(token, str)
                or not re.fullmatch(r"[0-9a-f]{32}", token)
            ):
                continue
            tool = AcpMcpTool(
                ToolCard(name=name, description=description, input_params=schema),
                request.session_id, token,
            )
            adapter._register_agent_owned_tool(tool, adapter._tool_owner_id())
            manager.add(tool.card)
            registered.add(name)
        yield
    finally:
        for name in registered:
            manager.remove_ability(name)
        if registered:
            adapter._prune_tool_cards(registered)
