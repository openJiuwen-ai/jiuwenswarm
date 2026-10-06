# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Electron and managed Chrome coexist without process-wide backend switches."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    BrowserRunGuardrails,
    RuntimeSettings,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.service import BrowserService

from jiuwenswarm.agents.harness.common import browser_config, electron_sideview
from jiuwenswarm.agents.swarm import browser_runtime
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import code_subagents
from jiuwenswarm.common.playwright_mcp_runtime import PlaywrightMcpLaunch
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "BROWSER_", "PLAYWRIGHT_", "JIUWENSWARM_ELECTRON", "JIUWENSWARM_BROWSER_",
    ))}
    env.update(
        BROWSER_DRIVER="remote",
        PLAYWRIGHT_MCP_TARGET_RESOLVER="http://127.0.0.1:43124",
        PLAYWRIGHT_MCP_COMMAND="electron.exe",
        PLAYWRIGHT_MCP_ARGS='["target_mcp_wrapper.cjs"]',
        JIUWENSWARM_ELECTRON="1",
        JIUWENSWARM_DATA_DIR=str(tmp_path),
    )
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(browser_runtime, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(browser_runtime, "resolve_playwright_mcp_launch", lambda *, environ: (
        PlaywrightMcpLaunch("bundled", "node", ("/bundled/cli.js",), "0.0.78")
        if environ == {} else pytest.fail("must not inherit the Electron launch override")
    ))


@pytest.fixture
def settings():
    return RuntimeSettings(
        provider="OpenAI", api_key="synthetic-not-used", api_base="https://example.invalid/v1",
        model_name="synthetic-not-used", guardrails=BrowserRunGuardrails(),
        instance=BrowserInstanceConfig(key="original"),
        mcp_cfg=McpServerConfig(
            server_id="original", server_name="original", server_path="stdio://playwright", client_type="stdio",
            params={"command": "electron.exe", "args": ["target_mcp_wrapper.cjs"], "timeout_s": 30,
                    "env": {"PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:43123",
                            "PLAYWRIGHT_MCP_TARGET_RESOLVER": "http://127.0.0.1:43124",
                            "PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN": "synthetic-token",
                            "PLAYWRIGHT_MCP_TARGET_ID": "old-id", "PLAYWRIGHT_MCP_SESSION_ID": "old-session",
                            "ELECTRON_RUN_AS_NODE": "1", "HTTP_PROXY": "http://proxy.invalid:8080",
                            "NO_PROXY": "127.0.0.1,localhost"}},
        ),
    )


@pytest.mark.parametrize("platform,key", [
    ("win32", "windows"), ("cygwin", "windows"), ("darwin", "macos"), ("linux", "linux"),
])
def test_platform_path_resolution_matches_browser_settings(monkeypatch, platform, key):
    monkeypatch.setattr(browser_config.sys, "platform", platform)
    assert browser_config.resolve_chrome_path({"browser": {"chrome_path": {key: " /chrome ", "default": "/other"}}}) == "/chrome"
    assert browser_config.resolve_chrome_path({"browser": {"chrome_path": {key: " ", "default": "/other"}}}) == "/other"


@pytest.mark.parametrize("config", [None, {}, {"browser": None}, {"browser": {"chrome_path": 3}}])
def test_missing_or_invalid_configuration_does_not_select_external(config):
    assert browser_config.resolve_chrome_path(config) == ""


def test_path_environment_placeholder(monkeypatch):
    monkeypatch.setenv("TEST_CHROME_PATH", "/synthetic/chrome")
    assert browser_config.resolve_chrome_path({"browser": {"chrome_path": "${TEST_CHROME_PATH}"}}) == "/synthetic/chrome"


@pytest.mark.parametrize("path", ["", "   "])
def test_empty_path_retains_electron_member_binding(settings, path):
    configured = browser_runtime.apply_swarm_browser_settings(
        settings, {"browser": {"chrome_path": path}}, "one", member_id="alice",
    )
    assert configured.mcp_cfg.params["command"] == "electron.exe"
    assert configured.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_SESSION_ID"] == "one"
    assert configured.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_MEMBER_ID"] == "alice"


@pytest.mark.parametrize("driver", ["managed", "extension", "remote"])
def test_non_electron_launch_is_unchanged(monkeypatch, settings, driver):
    monkeypatch.delenv("PLAYWRIGHT_MCP_TARGET_RESOLVER")
    monkeypatch.delenv("JIUWENSWARM_ELECTRON")
    monkeypatch.setenv("BROWSER_DRIVER", driver)
    assert browser_runtime.apply_swarm_browser_settings(
        settings, {"browser": {"chrome_path": "/chrome"}}, "one", member_id="alice",
    ) is settings


def test_external_members_are_isolated_and_do_not_mutate_electron(settings, tmp_path):
    original_env = dict(os.environ)
    original_params = settings.mcp_cfg.model_dump()
    pairs = [("one", "alice"), ("one", "bob"), ("two", "alice"), ("one-alice", "bob")]
    configured = [browser_runtime.apply_swarm_browser_settings(
        settings, {"browser": {"chrome_path": "/missing/chrome"}}, sid, member_id=member,
    ) for sid, member in pairs]
    assert len({item.mcp_cfg.server_id for item in configured}) == len(pairs)
    assert len({item.instance.user_data_dir for item in configured}) == len(pairs)
    for item in configured:
        assert item.instance.driver_mode == "managed"
        assert item.instance.managed_port == 0
        assert item.instance.browser_binary == "/missing/chrome"
        assert item.instance.cdp_url == ""
        assert item.instance.user_data_dir.startswith(str(tmp_path / ".browser-profiles"))
        assert item.mcp_cfg.server_name == "playwright-official"
        assert item.mcp_cfg.params["command"] == "node"
        assert item.mcp_cfg.params["args"][0] == "/bundled/cli.js"
        assert item.mcp_cfg.params["timeout_s"] == 30
        assert item.mcp_cfg.params["env"] == {"HTTP_PROXY": "http://proxy.invalid:8080", "NO_PROXY": "127.0.0.1,localhost"}
    assert settings.mcp_cfg.model_dump() == original_params
    assert dict(os.environ) == original_env
    single = electron_sideview.apply_session_sideview_target(settings, "single")
    assert single.mcp_cfg.params["command"] == "electron.exe"
    assert single.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_TARGET_RESOLVER"] == original_env["PLAYWRIGHT_MCP_TARGET_RESOLVER"]
    repeated = browser_runtime.apply_swarm_browser_settings(
        settings, {"browser": {"chrome_path": "/chrome"}}, "one", member_id="alice",
    )
    assert repeated.instance.user_data_dir == configured[0].instance.user_data_dir


@pytest.mark.parametrize("headless", [True, False])
def test_adapter_sync_preserves_electron_and_supplies_managed_display_mode(settings, headless):
    config = {"browser": {"chrome_path": "/chrome", "headless": headless}}
    adapter = JiuWenSwarmDeepAdapter()
    adapter._sync_browser_runtime_environment(config, runtime_enabled=True)
    configured = browser_runtime.apply_swarm_browser_settings(settings, config, "one", member_id="alice")
    service = BrowserService(
        provider=configured.provider, api_key=configured.api_key,
        api_base=configured.api_base, model_name=configured.model_name,
        mcp_cfg=configured.mcp_cfg, guardrails=configured.guardrails, instance=configured.instance,
    )
    profile = service._build_managed_profile()
    assert profile.driver_type == "managed"
    assert profile.browser_binary == "/chrome"
    assert ("--headless=new" in profile.extra_args) is headless
    assert os.environ["BROWSER_DRIVER"] == "remote"
    assert os.environ["PLAYWRIGHT_MCP_COMMAND"] == "electron.exe"
    assert os.environ["PLAYWRIGHT_MCP_ARGS"] == '["target_mcp_wrapper.cjs"]'
    adapter._sync_browser_runtime_environment({"browser": {"chrome_path": "", "headless": True}}, runtime_enabled=True)
    assert "BROWSER_MANAGED_ARGS" not in os.environ


def test_provider_uses_saved_config_each_time_and_can_restore_electron(monkeypatch, settings):
    monkeypatch.setattr(code_subagents, "build_browser_agent_config", lambda *args, **kwargs: (
        SimpleNamespace(factory_kwargs={"settings": settings})
    ))
    ctx = SwarmBuildContext(mode="code.team", role="teammate", member_name="alice", session_id="one",
                            config={"browser": {"chrome_path": "/chrome"}})
    ctx.extras["_parent_model"] = object()
    external = code_subagents.build_swarm_browser_agent({}, ctx)
    assert external.factory_kwargs["auto_create_workspace"] is False
    assert external.factory_kwargs["settings"].instance.driver_mode == "managed"
    ctx.config["browser"]["chrome_path"] = ""
    embedded = code_subagents.build_swarm_browser_agent({}, ctx)
    assert embedded.factory_kwargs["settings"].mcp_cfg.params["env"]["PLAYWRIGHT_MCP_MEMBER_ID"] == "alice"
