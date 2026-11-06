# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The rail of a PowerPoint task pane's conversation, one created with ``client_app`` ``office-powerpoint``.

Each conversation has its own agent, and only a task pane's conversation
loads the app template that declares the rail (``powerpoint/manifest.json``),
so the rail serves one conversation. Which tools its presentation's PowerPoint
runs can change when the task pane attaches again (another PowerPoint
version), so each model call reads them anew.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.harness.rails._multimodal import should_enable_read_image_multimodal

from jiuwenswarm.office.assets import OfficeAssetStore, PublishPowerPointAssetTool
from jiuwenswarm.office.client_tools import build_powerpoint_tools
from jiuwenswarm.office.documents import ListOpenDocumentsTool
from jiuwenswarm.office.runtime import session_id_of


class OfficeToolsRail(AgentRail):
    """Add the PowerPoint tools; show a model call the tools the presentation can run.

    The tools are the contract tools the task pane runs,
    ``publish_powerpoint_asset`` and ``list_open_documents``. A deferred tool
    the presentation cannot run still shows in ``tool_search``; calling it
    fails with the reason (``PowerPointTool.build_request``).
    """

    priority = 90

    def __init__(self) -> None:
        super().__init__()
        self._tools: list[Any] = []
        self._client_tool_names: frozenset[str] = frozenset()

    def init(self, agent: Any) -> None:
        store = OfficeAssetStore()
        client_tools = build_powerpoint_tools(partial(should_enable_read_image_multimodal, agent), store)
        self._client_tool_names = frozenset(tool.card.name for tool in client_tools)
        self._tools = [*client_tools, PublishPowerPointAssetTool(store), ListOpenDocumentsTool()]
        for tool in self._tools:
            agent.ability_manager.add_ability(tool.card, tool)

    def uninit(self, agent: Any) -> None:
        for tool in self._tools:
            agent.ability_manager.remove_ability(tool.card.name)
        self._tools = []
        self._client_tool_names = frozenset()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Hide the tools the presentation cannot run."""
        tools = getattr(ctx.inputs, "tools", None)
        if not isinstance(tools, list):
            return
        hidden = self._client_tool_names - _supported_tools(session_id_of(ctx.session))
        ctx.inputs.tools = [tool for tool in tools if _tool_name(tool) not in hidden]


def _supported_tools(session_id: str) -> frozenset[str]:
    from jiuwenswarm.server.client_attachments import load_client_capabilities

    capabilities = load_client_capabilities(session_id) if session_id else None
    return frozenset((capabilities or {}).get("supported_tools") or ())


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        return str((function if isinstance(function, dict) else tool).get("name") or "")
    return str(getattr(tool, "name", "") or "")
