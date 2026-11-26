# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expose tools owned by the connected ACP bridge for a single Runtime turn."""

import re
from collections.abc import Callable

from openjiuwen.core.foundation.tool import Tool, ToolCard

from jiuwenswarm.acp.session_mcp import MCP_CALL_METHOD, MCP_TOOLS_PARAM
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager

_TOOL_NAME = re.compile(r"mcp__[A-Za-z0-9_-]{1,59}")
_TOKEN = re.compile(r"[0-9a-f]{32}")


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


def _valid_declaration(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    name, token = item.get("name"), item.get("token")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        return False
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return False
    return isinstance(item.get("schema"), dict) and isinstance(item.get("description"), str)


def _declarations(request) -> list | None:
    declarations = (request.params or {}).get(MCP_TOOLS_PARAM)
    if request.channel_id != "acp" or not request.session_id or not isinstance(declarations, list):
        return None
    return declarations


def declares_acp_mcp_tools(request) -> bool:
    """Return whether the request comes from an ACP session that declares bridge MCP tools."""
    return _declarations(request) is not None


def acp_mcp_tools(request, reserved: Callable[[], set[str]]) -> list[AcpMcpTool]:
    """Build the turn's bridge-owned MCP tools from the request declarations.

    Args:
        request: Runtime request carrying the declarations in its params.
        reserved: Returns the tool names already registered on the agent;
            declarations that collide with them are skipped. Only called for
            ACP session requests.

    Returns:
        One tool per valid, uniquely named declaration. Requests that do not
        come from an ACP session yield no tools.
    """
    declarations = _declarations(request)
    if declarations is None:
        return []
    tools = []
    names = set(reserved())
    for item in declarations:
        if not _valid_declaration(item) or item["name"] in names:
            continue
        names.add(item["name"])
        card = ToolCard(name=item["name"], description=item["description"], input_params=item["schema"])
        tools.append(AcpMcpTool(card, request.session_id, item["token"]))
    return tools
