"""Regression tests for request-scoped Custom Tool cleanup."""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.ability_manager import AbilityManager


def _stub_jieba_before_interface_deep_import() -> None:
    """Avoid the optional jieba dependency while importing interface_deep."""
    if "jieba" in sys.modules:
        return
    jieba = types.ModuleType("jieba")
    jieba.__path__ = []
    sys.modules["jieba"] = jieba
    sys.modules["jieba.finalseg"] = types.ModuleType("jieba.finalseg")
    sys.modules["jieba._compat"] = types.ModuleType("jieba._compat")


_stub_jieba_before_interface_deep_import()

interface_module = importlib.import_module(
    "jiuwenclaw.agentserver.deep_agent.interface_deep"
)
JiuWenClawDeepAdapter = interface_module.JiuWenClawDeepAdapter


class _ResourceManager:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_tool(self, tool: object) -> None:
        self.tools[tool.card.id] = tool

    def remove_tool(self, tool_id: str) -> None:
        self.tools.pop(tool_id, None)

    def get_tool(self, tool_id: str) -> object | None:
        return self.tools.get(tool_id)


def _tool(tool_id: str, name: str = "custom_tool") -> SimpleNamespace:
    return SimpleNamespace(
        card=ToolCard(
            id=tool_id,
            name=name,
            description="test tool",
            input_params={"type": "object"},
        )
    )


def _valid_context() -> dict[str, object]:
    return {
        "enabled": True,
        "provider_id": "provider-1",
        "client_session_id": "client-1",
        "resource": {"id": "doc-1", "type": "document", "version": 1},
        "tools": [
            {
                "name": "document.read",
                "description": "Read the document",
                "inputSchema": {"type": "object"},
            }
        ],
    }


def test_refresh_client_runtime_tool_removes_only_previous_execution(
    monkeypatch,
) -> None:
    resource_mgr = _ResourceManager()
    ability_mgr = AbilityManager()
    previous = _tool("custom-tool-old")
    replacement = _tool("custom-tool-new")
    other_agent_tool = _tool("custom-tool-other-agent")
    resource_mgr.add_tool(previous)
    resource_mgr.add_tool(other_agent_tool)
    ability_mgr.add(previous.card)

    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=ability_mgr)
    monkeypatch.setattr(
        interface_module.Runner,
        "resource_mgr",
        resource_mgr,
    )
    monkeypatch.setattr(
        interface_module,
        "get_client_tool",
        lambda **_kwargs: replacement,
    )

    adapter._refresh_client_runtime_tool(
        session_id="session-1",
        request_id="request-2",
        channel_id="web",
        request_metadata={"custom_tool_context": _valid_context()},
    )

    assert resource_mgr.get_tool(previous.card.id) is None
    assert resource_mgr.get_tool(replacement.card.id) is replacement
    assert resource_mgr.get_tool(other_agent_tool.card.id) is other_agent_tool
    assert ability_mgr.get("custom_tool") is replacement.card


def test_refresh_client_runtime_tool_removes_previous_when_context_is_absent(
    monkeypatch,
) -> None:
    resource_mgr = _ResourceManager()
    ability_mgr = AbilityManager()
    previous = _tool("custom-tool-old")
    resource_mgr.add_tool(previous)
    ability_mgr.add(previous.card)

    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=ability_mgr)
    monkeypatch.setattr(
        interface_module.Runner,
        "resource_mgr",
        resource_mgr,
    )

    adapter._refresh_client_runtime_tool(
        session_id="session-1",
        request_id="request-2",
        channel_id="web",
        request_metadata=None,
    )

    assert resource_mgr.get_tool(previous.card.id) is None
    assert ability_mgr.get("custom_tool") is None
