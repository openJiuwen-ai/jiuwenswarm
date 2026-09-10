# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configured-provider exposure for main-agent reload and MCP subagents."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from weakref import WeakSet

import pytest

from jiuwenswarm.agents.harness.common.tools import mcp_toolkits, search_tools
from jiuwenswarm.agents.harness.common.tools.search_tools import mcp_paid_search
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def isolated_search_env(monkeypatch):
    for provider in ("bocha", "perplexity", "serper", "jina"):
        monkeypatch.setenv(f"{provider.upper()}_API_KEY", "")
    monkeypatch.setattr(mcp_paid_search.card, "description", mcp_paid_search.card.description)
    monkeypatch.setattr(mcp_paid_search.card, "input_params", deepcopy(mcp_paid_search.card.input_params))
    monkeypatch.setattr(mcp_toolkits, "_SEARCH_ABILITY_MANAGERS", WeakSet())


@pytest.mark.parametrize("adapter_class", [JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter])
def test_reload_refreshes_provider_metadata_and_removes_disabled_tool(monkeypatch, adapter_class):
    adapter = object.__new__(adapter_class)
    adapter._paid_search_tool = None
    adapter._paid_search_registered = False
    adapter._tool_cards = []
    cards = {}
    registered = {}
    adapter._instance = SimpleNamespace(ability_manager=SimpleNamespace(
        add=lambda card: cards.update({card.name: card}),
        remove=lambda name: cards.pop(name, None),
    ))
    adapter._tool_owner_id = lambda: "test-owner"
    adapter._resolve_runtime_language = lambda: "en"
    adapter._active_code_config = lambda: {"modes": {"code": {"tools": ["web_paid_search"]}}}
    monkeypatch.setattr(interface_deep, "register_tool", lambda tool, owner: registered.update({tool.card.name: tool}))
    monkeypatch.setattr(interface_deep, "unregister_tool", lambda tool: registered.pop(tool.card.name, None))

    adapter.refresh_paid_search_tool_for_runtime()
    assert adapter._paid_search_tool is None
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    adapter.refresh_paid_search_tool_for_runtime()
    first = adapter._paid_search_tool
    assert cards["paid_search"].input_params["properties"]["provider"]["enum"] == ["auto", "bocha"]

    adapter.refresh_paid_search_tool_for_runtime()
    assert adapter._paid_search_tool is first
    monkeypatch.setenv("BOCHA_API_KEY", " \t ")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    adapter.refresh_paid_search_tool_for_runtime()
    assert adapter._paid_search_tool is not first
    assert adapter._paid_search_tool.card.id == first.card.id
    assert cards["paid_search"].input_params["properties"]["provider"]["enum"] == ["auto", "serper"]
    assert "bocha" not in cards["paid_search"].description.lower()
    assert adapter._tool_cards == [cards["paid_search"]]
    assert registered["paid_search"] is adapter._paid_search_tool

    monkeypatch.setenv("SERPER_API_KEY", "")
    adapter.refresh_paid_search_tool_for_runtime()
    assert not adapter._paid_search_registered
    assert adapter._paid_search_tool is None
    assert adapter._tool_cards == []
    assert cards == registered == {}


@pytest.mark.parametrize("adapter_class", [JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter])
def test_paid_search_refresh_skips_uninitialized_runtime(adapter_class):
    adapter = object.__new__(adapter_class)
    adapter._instance = None
    adapter._sync_paid_search_tool_for_runtime = MagicMock()

    adapter.refresh_paid_search_tool_for_runtime()

    adapter._sync_paid_search_tool_for_runtime.assert_not_called()
    assert adapter._instance is None


def test_code_mode_with_paid_search_disabled_stays_disabled(monkeypatch):
    adapter = object.__new__(JiuwenSwarmCodeAdapter)
    adapter._instance = object()
    adapter._paid_search_tool = None
    adapter._paid_search_registered = False
    adapter._active_code_config = lambda: {"modes": {"code": {"tools": []}}}
    adapter._tool_owner_id = lambda: "test-owner"
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    adapter.refresh_paid_search_tool_for_runtime()
    assert adapter._paid_search_tool is None
    assert not adapter._paid_search_registered


def test_mcp_metadata_tracks_provider_configuration(monkeypatch):
    assert mcp_paid_search not in mcp_toolkits.get_mcp_tools()
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    assert mcp_paid_search in mcp_toolkits.get_mcp_tools()
    assert mcp_paid_search.card.input_params["properties"]["provider"]["enum"] == ["auto", "bocha"]
    assert "serper" not in mcp_paid_search.card.description.lower()
    monkeypatch.setenv("BOCHA_API_KEY", " ")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    assert mcp_paid_search in mcp_toolkits.get_mcp_tools()
    assert mcp_paid_search.card.input_params["properties"]["provider"]["enum"] == ["auto", "serper"]
    assert "bocha" not in mcp_paid_search.card.description.lower()
    monkeypatch.setenv("SERPER_API_KEY", "")
    assert mcp_paid_search not in mcp_toolkits.get_mcp_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bocha", "perplexity", "serper", "jina"])
async def test_registered_mcp_provider_requests_use_rotated_key(monkeypatch, provider):
    key_name = f"{provider.upper()}_API_KEY"
    monkeypatch.setenv(key_name, "first-test-key")
    tool = next(item for item in mcp_toolkits.get_mcp_tools() if item.card.name == "mcp_paid_search")
    response = MagicMock()
    response.json.return_value = {
        "summary": "answer", "webPages": {"value": [{"url": "https://example.invalid/result"}]},
        "choices": [{"message": {"content": "answer https://example.invalid/result"}}],
        "citations": ["https://example.invalid/result"],
        "organic": [{"link": "https://example.invalid/result"}],
    }
    request = MagicMock(return_value=response)
    monkeypatch.setattr(search_tools, "_http_request", request)
    for key in ("first-test-key", "rotated-test-key"):
        monkeypatch.setenv(key_name, key)
        result = await tool.invoke({"query": "test", "provider": provider})
        assert f"Paid search provider: {provider}" in result
        assert "https://example.invalid/result" in result
        headers = request.call_args.kwargs["headers"]
        if provider == "serper":
            assert headers["X-API-KEY"] == key
        else:
            assert headers["Authorization"] == f"Bearer {key}"
    assert request.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["auto", "serper"])
async def test_mcp_dispatch_never_enters_unconfigured_runner(monkeypatch, provider):
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    available = MagicMock(return_value={"answer": "answer", "urls": []})
    unavailable = MagicMock(side_effect=AssertionError("Unconfigured provider was dispatched"))
    monkeypatch.setattr(search_tools, "_bocha_search_sync", available)
    for name in ("serper", "perplexity", "jina"):
        monkeypatch.setattr(search_tools, f"_{name}_search_sync", unavailable)
    used, answer, _ = await search_tools.run_paid_search_structured("test", provider=provider)
    assert (used, answer) == ("bocha", "answer")
    available.assert_called_once()
    unavailable.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_call_queued_before_key_change_uses_current_provider(monkeypatch):
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    mcp_toolkits.get_mcp_tools()
    monkeypatch.setenv("BOCHA_API_KEY", "")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    mcp_toolkits.refresh_mcp_paid_search_tools()
    available = MagicMock(return_value={"answer": "answer", "urls": []})
    unavailable = MagicMock(side_effect=AssertionError("Removed provider was dispatched"))
    monkeypatch.setattr(search_tools, "_serper_search_sync", available)
    monkeypatch.setattr(search_tools, "_bocha_search_sync", unavailable)
    result = await mcp_paid_search.invoke({"query": "test", "provider": "bocha"})
    assert "Paid search provider: serper" in result
    available.assert_called_once()
    unavailable.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_provider_filter_preserves_input_normalization_and_clamping(monkeypatch):
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    mcp_toolkits.get_mcp_tools()
    runner = MagicMock(return_value={"answer": "answer", "urls": []})
    monkeypatch.setattr(search_tools, "_bocha_search_sync", runner)
    result = await mcp_paid_search.invoke({
        "query": " test ", "provider": " BOCHA ", "max_results": 99, "timeout_seconds": 1,
    })
    assert "Paid search provider: bocha" in result
    runner.assert_called_once_with(query="test", max_results=20, timeout_seconds=10)


@pytest.mark.asyncio
async def test_mcp_no_keys_does_not_dispatch(monkeypatch):
    runner = MagicMock(side_effect=AssertionError("No configured provider"))
    for name in ("bocha", "perplexity", "serper", "jina"):
        monkeypatch.setattr(search_tools, f"_{name}_search_sync", runner)
    with pytest.raises(RuntimeError, match="no paid search API keys configured"):
        await search_tools.run_paid_search_structured("test")
    runner.assert_not_called()


def test_release_mcp_exports_preserve_tool_identity():
    assert mcp_toolkits.mcp_free_search is search_tools.mcp_free_search
    assert mcp_toolkits.mcp_paid_search is search_tools.mcp_paid_search


@pytest.mark.asyncio
async def test_release_free_search_result_contract(monkeypatch):
    search = MagicMock(return_value=(
        "duckduckgo", [{"title": "Result", "url": "https://example.invalid/a", "snippet": "Summary"}],
    ))
    monkeypatch.setattr(search_tools, "_search_free_sync", search)

    result = await search_tools.mcp_free_search.invoke({
        "query": " test ", "max_results": 99, "timeout_seconds": 1,
    })

    search.assert_called_once_with("test", 20, 5)
    assert result == (
        "Free search results (DuckDuckGo) for: test\n"
        "1. Result\n   URL: https://example.invalid/a\n   Snippet: Summary"
    )


@pytest.mark.asyncio
async def test_release_paid_search_returns_text_error_without_keys(monkeypatch):
    runner = MagicMock(side_effect=AssertionError("No configured provider"))
    for name in ("bocha", "perplexity", "serper", "jina"):
        monkeypatch.setattr(search_tools, f"_{name}_search_sync", runner)
    mcp_toolkits.refresh_mcp_paid_search_tools()

    result = await mcp_paid_search.invoke({"query": "test"})

    assert result == "[ERROR]: no paid search API keys configured."
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_fallback_skips_provider_disabled_during_previous_attempt(monkeypatch):
    for provider in ("BOCHA", "SERPER", "JINA"):
        monkeypatch.setenv(f"{provider}_API_KEY", "test-key")

    def fail_first_provider(**kwargs):
        monkeypatch.setenv("SERPER_API_KEY", "")
        raise RuntimeError("First provider unavailable")

    disabled = MagicMock(side_effect=AssertionError("Disabled provider was dispatched"))
    available = MagicMock(return_value={"answer": "answer", "urls": []})
    monkeypatch.setattr(search_tools, "_bocha_search_sync", fail_first_provider)
    monkeypatch.setattr(search_tools, "_serper_search_sync", disabled)
    monkeypatch.setattr(search_tools, "_jina_search_sync", available)

    used, answer, _ = await search_tools.run_paid_search_structured("test")

    assert (used, answer) == ("jina", "answer")
    disabled.assert_not_called()
    available.assert_called_once()


@pytest.mark.asyncio
async def test_existing_mcp_agent_refreshes_model_tools_on_enable_change_and_disable(monkeypatch):
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    manager = AbilityManager()
    mcp_toolkits.track_mcp_search_tools(manager)
    resource_add = MagicMock()
    monkeypatch.setattr(mcp_toolkits.Runner.resource_mgr, "add_tool", resource_add)
    for provider in ("bocha", "serper"):
        monkeypatch.setenv("BOCHA_API_KEY", "")
        monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
        mcp_toolkits.refresh_mcp_paid_search_tools()
        info = next(item for item in await manager.list_tool_info() if item.name == "mcp_paid_search")
        assert info.parameters["properties"]["provider"]["enum"] == ["auto", provider]
    monkeypatch.setenv("SERPER_API_KEY", "")
    mcp_toolkits.refresh_mcp_paid_search_tools()
    assert manager.get("mcp_paid_search") is None
    assert not any(item.name == "mcp_paid_search" for item in await manager.list_tool_info())
    assert resource_add.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [None, set(), {"search"}, {"search", "model"}])
async def test_search_reload_updates_active_sessions_before_lazy_full_reload(scopes):
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    calls = MagicMock()
    root._session_adapters = {
        "first": SimpleNamespace(refresh_paid_search_tool_for_runtime=calls.refresh_first),
        "second": SimpleNamespace(refresh_paid_search_tool_for_runtime=calls.refresh_second),
    }
    root._mark_session_adapters_stale_for_reload = calls.mark_stale
    await root._fan_out_reload_to_session_adapters({}, {}, None, scopes)
    assert calls.mock_calls == [
        call.refresh_first(),
        call.refresh_second(),
        call.mark_stale({}, {}, scopes),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [{"model"}, {"multimodal"}])
async def test_unrelated_reload_does_not_refresh_session_paid_search(scopes):
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    refresh = MagicMock()
    root._session_adapters = {
        "session": SimpleNamespace(refresh_paid_search_tool_for_runtime=refresh),
    }
    root._mark_session_adapters_stale_for_reload = MagicMock()

    await root._fan_out_reload_to_session_adapters({}, {}, None, scopes)

    refresh.assert_not_called()
    root._mark_session_adapters_stale_for_reload.assert_called_once_with({}, {}, scopes)
