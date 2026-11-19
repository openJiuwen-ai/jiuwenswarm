# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Free-search settings persist, reload, and enable each serving entry point."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from weakref import WeakSet

import pytest
from dotenv import dotenv_values

from jiuwenswarm.common import utils
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.server.control import config_service


FIELDS = (
    ("free_search_ddg_enabled", "FREE_SEARCH_DDG_ENABLED"),
    ("free_search_bing_enabled", "FREE_SEARCH_BING_ENABLED"),
)
FLAGS = tuple(flag for _, flag in FIELDS)
REMOVED_FIELD = "free_search_360_enabled"
REMOVED_FLAG = "FREE_SEARCH_360_ENABLED"


@pytest.fixture
def search_settings(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep unrelated configuration\nUNRELATED_SETTING=preserved\n", encoding="utf-8")
    for flag in FLAGS:
        monkeypatch.setenv(flag, "false")
    monkeypatch.delenv(REMOVED_FLAG, raising=False)
    for name in ("BOCHA_API_KEY", "SERPER_API_KEY", "JINA_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(handlers, "ENV_FILE", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(handlers, "get_config", lambda: {})
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(config_service, "get_env_file", lambda: path)
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    return path


@pytest.mark.parametrize("value", ["true", "false", True, False])
@pytest.mark.parametrize("field, flag", FIELDS)
@pytest.mark.asyncio
async def test_search_save_read_reload_round_trip(search_settings, monkeypatch, value, field, flag):
    channel = SimpleNamespace(send_response=AsyncMock())
    reload = AsyncMock(return_value=True)
    await handlers.config_save_all_handler(
        channel, None, "save-search", {"config": {field: value}}, None,
        on_config_saved=reload,
    )
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is True
    assert response["payload"]["updated"] == [field]
    assert response["payload"]["applied_without_restart"] is True
    assert reload.await_args.kwargs["reload_options"]["reload_scopes"] == ["search"]
    saved = dotenv_values(search_settings)
    assert saved[flag].lower() == str(value).lower()
    assert saved["UNRELATED_SETTING"] == "preserved"
    assert "# keep unrelated configuration" in search_settings.read_text(encoding="utf-8")
    assert all(os.environ[name] == "false" for name in FLAGS if name != flag)
    assert REMOVED_FLAG not in saved

    await handlers.config_get_handler(channel, None, "read-search", {}, None)
    assert channel.send_response.call_args.kwargs["payload"][field].lower() == str(value).lower()
    # Front is a different process: persisted values must beat its stale env.
    monkeypatch.setenv(flag, "false" if str(value).lower() == "true" else "true")
    assert config_service.get_panel()[field].lower() == str(value).lower()


@pytest.mark.parametrize("broken_yaml", [False, True])
@pytest.mark.asyncio
async def test_removed_setting_is_absent_in_both_read_paths(search_settings, monkeypatch, broken_yaml):
    monkeypatch.setenv(REMOVED_FLAG, "true")
    search_settings.write_text(f"{REMOVED_FLAG}=true\n", encoding="utf-8")
    if broken_yaml:
        def fail():
            raise ValueError("invalid test YAML")
        monkeypatch.setattr(config_service, "_read_raw_config", fail)
        monkeypatch.setattr(handlers, "get_config_raw", fail)
    panel = config_service.get_panel()
    assert REMOVED_FIELD not in panel
    assert all(panel[field] == "false" for field, _ in FIELDS)
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "read-search", {}, None)
    payload = channel.send_response.call_args.kwargs["payload"]
    assert REMOVED_FIELD not in payload
    assert all(payload[field] == "false" for field, _ in FIELDS)


@pytest.mark.parametrize("flag", FLAGS)
def test_free_engine_change_requests_global_search_reload(flag):
    assert handlers.ConfigChangeSet({flag: "true"}, []).reload_scopes == {"search"}


@pytest.mark.parametrize("value", [None, "", "false", "true"])
def test_runtime_defaults_do_not_create_removed_setting(search_settings, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(REMOVED_FLAG, raising=False)
    else:
        monkeypatch.setenv(REMOVED_FLAG, value)
    utils.apply_free_search_runtime_defaults()
    assert os.environ.get(REMOVED_FLAG) == value
    assert all(os.environ[flag] == "false" for flag in FLAGS)


@pytest.mark.asyncio
async def test_removed_flag_does_not_enable_sdk_or_legacy_tools(search_settings, monkeypatch):
    from openjiuwen.harness.tools.web import _http, is_free_search_enabled
    from jiuwenswarm.agents.harness.common.tools import mcp_toolkits
    from jiuwenswarm.agents.harness.common.tools.trusted_search_tool_adapter import mcp_free_search
    from jiuwenswarm.server.runtime.agent_adapter.trusted_web_search import TrustedWebFreeSearchTool

    monkeypatch.setenv(REMOVED_FLAG, "true")
    runner = AsyncMock(side_effect=AssertionError("disabled source dispatched"))
    monkeypatch.setattr(_http, "request", runner)
    assert not is_free_search_enabled()
    assert mcp_free_search not in mcp_toolkits.get_mcp_tools()
    result = await TrustedWebFreeSearchTool().invoke({"query": "Python documentation", "max_results": 5})
    assert "Content status: disabled" in result
    result = await mcp_free_search.invoke({"query": "Python documentation", "max_results": 5})
    assert "all free search engines are disabled" in result
    runner.assert_not_awaited()


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.asyncio
async def test_search_toggle_refreshes_existing_mcp_agent(search_settings, monkeypatch, flag):
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from jiuwenswarm.agents.harness.common.tools import mcp_toolkits

    monkeypatch.setattr(mcp_toolkits, "_SEARCH_ABILITY_MANAGERS", WeakSet())
    monkeypatch.setattr(mcp_toolkits.Runner.resource_mgr, "add_tool", MagicMock())
    manager = AbilityManager()
    mcp_toolkits.track_mcp_search_tools(manager)
    monkeypatch.setenv(REMOVED_FLAG, "true")
    for enabled in (True, False, True):
        monkeypatch.setenv(flag, str(enabled).lower())
        mcp_toolkits.refresh_mcp_paid_search_tools()
        names = {tool.name for tool in await manager.list_tool_info()}
        assert ("mcp_free_search" in names) is enabled
        assert "mcp_paid_search" not in names


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("value", ["true", "ON", " 1 "])
def test_recommendation_accepts_any_one_free_engine(flag, value):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import background_configuration_status
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import selected_topic_search_provider_id

    env = {flag: value}
    assert selected_topic_search_provider_id(environ=env) == "openjiuwen-native-free-search"
    status = background_configuration_status(None, environ=env)
    assert status["native_search_missing"] == []
    assert sum(status["native_search"].values()) == 1


@pytest.mark.parametrize("env", [{}, {REMOVED_FLAG: "true"}])
def test_recommendation_lane_stays_ready_without_free_engine(env):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig, background_configuration_status,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        selected_topic_search_provider_id,
    )
    assert selected_topic_search_provider_id(environ=env) == "openjiuwen-native-free-search"
    config = ArkBackgroundConfig(api_key="test", model="test", context_window_tokens=131072)
    status = background_configuration_status(config, environ=env)
    assert status["status"] == "ready"
    assert status["native_search_missing"] == [
        "FREE_SEARCH_DDG_ENABLED=true OR FREE_SEARCH_BING_ENABLED=true",
    ]
    assert not any(status["native_search"].values())
    assert "360" not in status["native_search"]


@pytest.mark.asyncio
async def test_recommendation_without_free_engine_does_not_send_search_requests(search_settings, monkeypatch):
    from openjiuwen.harness.tools.web import _http
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        OpenJiuwenNativeSearchProviderError, build_openjiuwen_native_search,
    )

    runner = AsyncMock(side_effect=AssertionError("disabled source dispatched"))
    monkeypatch.setattr(_http, "request", runner)
    search = build_openjiuwen_native_search()
    with pytest.raises(OpenJiuwenNativeSearchProviderError):
        await search("Python", 3, 10)
    runner.assert_not_awaited()
    assert search.invocation_receipts == []


@pytest.mark.parametrize("engine", ["duckduckgo", "duckduckgo-jina", "bing"])
@pytest.mark.asyncio
async def test_recommendation_accepts_supported_receipt(engine):
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import build_openjiuwen_native_search

    runner = AsyncMock(return_value=(engine, [
        {"title": "Python", "url": "https://python.org/", "snippet": "Python documentation"},
    ]))
    search = build_openjiuwen_native_search(environ={flag: "true" for flag in FLAGS}, search_runner=runner)
    result = await search("Python", 3, 10)
    assert result.engine == engine
    assert result.receipt["engine"] == engine
    assert result.receipt["result_urls"] == ["https://python.org/"]


@pytest.mark.asyncio
async def test_recommendation_rejects_removed_provider_receipt():
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        OpenJiuwenNativeSearchContractError, build_openjiuwen_native_search,
    )

    runner = AsyncMock(return_value=("360", []))
    search = build_openjiuwen_native_search(environ={FLAGS[0]: "true"}, search_runner=runner)
    with pytest.raises(OpenJiuwenNativeSearchContractError, match="search_result_contract_failed"):
        await search("Python", 3, 10)


@pytest.mark.asyncio
async def test_removed_flag_does_not_dispatch_legacy_search(search_settings, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools import search_tools

    monkeypatch.setenv(REMOVED_FLAG, "true")
    runner = MagicMock(side_effect=AssertionError("disabled source dispatched"))
    monkeypatch.setattr(search_tools, "_search_duckduckgo_sync", runner)
    monkeypatch.setattr(search_tools, "_search_duckduckgo_via_jina_sync", runner)
    monkeypatch.setattr(search_tools, "_search_bing_sync", runner)
    with pytest.raises(RuntimeError, match="all free search engines are disabled"):
        await asyncio.to_thread(search_tools.run_free_search_structured, "Python", 3, 10)
    runner.assert_not_called()
