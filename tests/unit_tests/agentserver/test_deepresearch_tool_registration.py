"""Contract tests for the formal DeepResearch tool surface."""

import builtins
import logging
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools import deepresearch as dr
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_formal_tool_surface_includes_single_interactive_entry():
    assert [tool.card.name for tool in dr.get_deepresearch_tools()] == [
        "deepresearch_execute",
        "deepresearch_stream",
        "deepresearch_prepare_rewrite",
        "deepresearch_commit_rewrite",
        "deepresearch_generate_rewrite_html",
    ]


def test_formal_tools_honor_enable_flag(monkeypatch):
    monkeypatch.setattr(dr, "enable_deepresearch", lambda: False)
    assert dr.get_deepresearch_tools() == []


@pytest.mark.parametrize(
    ("config", "expected"),
    [({}, True), ({"enable_deepresearch": True}, True),
     ({"enable_deepresearch": False}, False),
     ({"enable_deepresearch": "false"}, False),
     ({"enable_deepresearch": 1}, False)],
)
def test_enable_gate_accepts_only_boolean(monkeypatch, config, expected):
    monkeypatch.setattr(dr, "get_config", lambda: config)
    assert dr.enable_deepresearch() is expected


def test_enable_gate_fails_closed_on_config_error(monkeypatch):
    monkeypatch.setattr(
        dr, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("bad config"))
    )
    assert dr.enable_deepresearch() is False


def test_formal_tool_registration_does_not_import_sdk(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith("openjiuwen_deepsearch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(dr, "enable_deepresearch", lambda: True)
    assert len(dr.get_deepresearch_tools()) == 5


def test_legacy_six_interface_names_remain_absent():
    names = {tool.card.name for tool in dr.get_deepresearch_tools()}
    assert names.isdisjoint({
        "deepresearch_start",
        "deepresearch_resume",
        "deepresearch_status",
        "deepresearch_report",
        "deepresearch_cancel",
        "deepresearch_outline",
    })


def test_adapter_registers_formal_tools_as_shared(monkeypatch):
    tools = [
        SimpleNamespace(card=SimpleNamespace(name=name))
        for name in (
            "deepresearch_execute",
            "deepresearch_stream",
            "deepresearch_prepare_rewrite",
            "deepresearch_commit_rewrite",
            "deepresearch_generate_rewrite_html",
        )
    ]
    registered_names: list[str] = []
    adapter = object.__new__(JiuWenSwarmDeepAdapter)

    def _register_shared(tool):
        registered_names.append(tool.card.name)
        return tool

    monkeypatch.setattr(interface_deep, "get_deepresearch_tools", lambda: tools)
    monkeypatch.setattr(adapter, "_register_shared_tool", _register_shared)
    cards: list[object] = []

    adapter._register_deepresearch_tool_cards(cards)

    assert registered_names == [tool.card.name for tool in tools]
    assert [card.name for card in cards] == registered_names


def test_adapter_registers_no_deepresearch_tools_when_disabled(monkeypatch):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)

    def register_shared(tool):
        pytest.fail(f"unexpected registration: {tool}")

    monkeypatch.setattr(interface_deep, "get_deepresearch_tools", lambda: [])
    monkeypatch.setattr(adapter, "_register_shared_tool", register_shared)
    cards: list[object] = []

    adapter._register_deepresearch_tool_cards(cards)

    assert cards == []


def test_adapter_registration_failure_does_not_return_partial_cards_or_log_secret(
    monkeypatch,
    caplog,
):
    tools = [
        SimpleNamespace(card=SimpleNamespace(name=f"deepresearch_{index}"))
        for index in range(4)
    ]
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    calls = 0

    def register_shared(tool):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("secret-api-key")
        return tool

    monkeypatch.setattr(interface_deep, "get_deepresearch_tools", lambda: tools)
    monkeypatch.setattr(adapter, "_register_shared_tool", register_shared)
    cards = [SimpleNamespace(name="existing")]

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="secret-api-key"):
        adapter._register_deepresearch_tool_cards(cards)

    assert [card.name for card in cards] == ["existing"]
    assert "secret-api-key" not in caplog.text
