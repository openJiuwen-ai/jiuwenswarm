# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The rail of a task pane's conversation, one created with the ``client_app`` of an Office app.

Each conversation has its own agent, and only a task pane's conversation
loads its app's template, which declares the rail (the ``manifest.json`` of
the app's package), so the rail serves one conversation. Which tools the
document's host runs can change when the task pane attaches again (another
host version), so each model call reads them anew.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.harness.rails._multimodal import should_enable_read_image_multimodal

from jiuwenswarm.office.assets import OfficeAssetStore, PublishOfficeAssetTool
from jiuwenswarm.office.client_tools import build_task_pane_tools
from jiuwenswarm.office.documents import ListOpenDocumentsTool
from jiuwenswarm.office.runtime import session_id_of


class OfficeToolsRail(AgentRail):
    """Add the app's tools; show a model call the tools the document's host can run.

    The tools are the contract tools of the app's package, which the task pane
    runs, ``publish_office_asset`` and ``list_open_documents``. A deferred tool
    the host cannot run still shows in ``tool_search``; calling it fails with
    the reason (``TaskPaneTool.build_request``).
    """

    priority = 90

    def __init__(self, package_dir: Path) -> None:
        super().__init__()
        self._package_dir = package_dir
        self._tools: list[Any] = []
        self._client_tool_names: frozenset[str] = frozenset()

    @classmethod
    def from_plugin_context(cls, *, params: dict[str, Any], context: Any) -> "OfficeToolsRail":
        """The rail of the template being loaded: its app's package is the directory of the ``manifest.json``."""
        return cls(Path(context.extras["source_root"]))

    def init(self, agent: Any) -> None:
        store = OfficeAssetStore()
        client_tools = build_task_pane_tools(self._package_dir, partial(_native_image_model, agent), store)
        self._client_tool_names = frozenset(tool.card.name for tool in client_tools)
        self._tools = [*client_tools, PublishOfficeAssetTool(store), ListOpenDocumentsTool()]
        for tool in self._tools:
            agent.ability_manager.add_ability(tool.card, tool)

    def uninit(self, agent: Any) -> None:
        for tool in self._tools:
            agent.ability_manager.remove_ability(tool.card.name)
        self._tools = []
        self._client_tool_names = frozenset()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Hide the tools the document's host cannot run."""
        tools = getattr(ctx.inputs, "tools", None)
        if not isinstance(tools, list):
            return
        hidden = self._client_tool_names - _supported_tools(session_id_of(ctx.session))
        ctx.inputs.tools = [tool for tool in tools if _tool_name(tool) not in hidden]


def _native_image_model(agent: Any) -> Model | None:
    """The conversation's current model when it takes images, else ``None``; a model switch replaces it."""
    return agent.deep_config.model if should_enable_read_image_multimodal(agent) else None


def _supported_tools(session_id: str) -> frozenset[str]:
    from jiuwenswarm.server.client_attachments import load_client_capabilities

    capabilities = load_client_capabilities(session_id) if session_id else None
    return frozenset((capabilities or {}).get("supported_tools") or ())


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        return str((function if isinstance(function, dict) else tool).get("name") or "")
    return str(getattr(tool, "name", "") or "")
