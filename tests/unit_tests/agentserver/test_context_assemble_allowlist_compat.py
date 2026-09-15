# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility tests for ContextAssembleRail eager-tool allowlists."""

# pylint: disable=protected-access

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface_deep


def test_build_context_assemble_rail_uses_modern_signature(monkeypatch):
    class ModernContextAssembleRail:
        def __init__(self, *, disabled_tools=None, tool_name_allowlist=None):
            self.disabled_tools = disabled_tools
            self.tool_name_allowlist = tool_name_allowlist

    monkeypatch.setattr(
        interface_deep,
        "ContextAssembleRail",
        ModernContextAssembleRail,
    )

    rail = interface_deep._build_context_assemble_rail(
        disabled_tools=["memory_search"],
        tool_name_allowlist=["tools_search", "invoke_tool"],
    )

    assert rail.disabled_tools == ["memory_search"]
    assert rail.tool_name_allowlist == ["tools_search", "invoke_tool"]


def test_build_context_assemble_rail_falls_back_and_calls_setter(monkeypatch):
    class LegacyContextAssembleRail:
        def __init__(self, disabled_tools=None):
            self.disabled_tools = disabled_tools
            self.allowlist_calls = []

        def set_tool_name_allowlist(self, tool_names):
            self.allowlist_calls.append(tool_names)

    monkeypatch.setattr(
        interface_deep,
        "ContextAssembleRail",
        LegacyContextAssembleRail,
    )

    rail = interface_deep._build_context_assemble_rail(
        disabled_tools=["memory_search"],
        tool_name_allowlist=["tools_search", "invoke_tool"],
    )

    assert rail.disabled_tools == ["memory_search"]
    assert rail.allowlist_calls == [["tools_search", "invoke_tool"]]


def test_build_context_assemble_rail_falls_back_to_no_args(monkeypatch):
    class OldContextAssembleRail:
        def __init__(self):
            self.disabled_calls = []

        def update_disabled_tools(self, tool_names):
            self.disabled_calls.append(tool_names)

    monkeypatch.setattr(
        interface_deep,
        "ContextAssembleRail",
        OldContextAssembleRail,
    )

    rail = interface_deep._build_context_assemble_rail(
        disabled_tools=["memory_search"],
        tool_name_allowlist=["tools_search"],
    )

    assert rail.disabled_calls == [["memory_search"]]


def test_sync_context_assemble_tool_allowlist_sets_and_clears():
    calls = []
    assemble = SimpleNamespace(
        set_tool_name_allowlist=lambda tool_names: calls.append(tool_names),
    )
    progressive = SimpleNamespace(
        eager_tools=["tools_search", "invoke_tool", "bash"],
    )

    interface_deep._sync_context_assemble_tool_allowlist(assemble, progressive)
    interface_deep._sync_context_assemble_tool_allowlist(assemble, None)

    assert calls == [["tools_search", "invoke_tool", "bash"], None]


def test_sync_context_assemble_tool_allowlist_tolerates_missing_setter():
    interface_deep._sync_context_assemble_tool_allowlist(
        SimpleNamespace(),
        SimpleNamespace(eager_tools=["tools_search"]),
    )
