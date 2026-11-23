# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for examples/extensions/mcp_http_auth."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_EXTENSION_PATH = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "extensions"
    / "mcp_http_auth"
    / "extension.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mcp_http_auth_under_test", _EXTENSION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ext = _load()


def _settings(**overrides):
    base = {
        "enabled": True,
        "header_name": "Authorization",
        "token_prefix": "Bearer ",
        "token_env": "CORP_MCP_TOKEN",
        "servers": {"corp-mcp-gw": {}},
    }
    base.update(overrides)
    return ext.parse_settings(base)


def _ctx(server="corp-mcp-gw", method="tools/call"):
    return SimpleNamespace(server_name=server, method=method, headers={})


@pytest.mark.asyncio
async def test_env_token_is_injected(monkeypatch):
    monkeypatch.setenv("CORP_MCP_TOKEN", "abc")
    ctx = _ctx()
    await ext.make_handler(_settings())(ctx)
    assert ctx.headers == {"Authorization": "Bearer abc"}


@pytest.mark.asyncio
async def test_provider_wins_and_sees_method(monkeypatch):
    monkeypatch.setenv("CORP_MCP_TOKEN", "env-value")
    seen = []

    def provider(server, method):
        seen.append((server, method))
        return f"{server}-{method}"

    ctx = _ctx(method="tools/list")
    await ext.make_handler(_settings(), token_provider=provider)(ctx)
    assert seen == [("corp-mcp-gw", "tools/list")]
    assert ctx.headers == {"Authorization": "Bearer corp-mcp-gw-tools/list"}


@pytest.mark.asyncio
async def test_unlisted_server_is_skipped(monkeypatch):
    monkeypatch.setenv("CORP_MCP_TOKEN", "abc")
    ctx = _ctx(server="other-gw")
    await ext.make_handler(_settings())(ctx)
    assert ctx.headers == {}


@pytest.mark.asyncio
async def test_missing_token_leaves_headers_untouched(monkeypatch):
    monkeypatch.delenv("CORP_MCP_TOKEN", raising=False)
    ctx = _ctx()
    await ext.make_handler(_settings())(ctx)
    assert ctx.headers == {}


@pytest.mark.asyncio
async def test_provider_failure_is_swallowed(monkeypatch):
    def provider(server, method):
        raise RuntimeError("sts down")

    ctx = _ctx()
    await ext.make_handler(_settings(), token_provider=provider)(ctx)
    assert ctx.headers == {}


@pytest.mark.asyncio
async def test_disabled_extension_is_noop(monkeypatch):
    monkeypatch.setenv("CORP_MCP_TOKEN", "abc")
    ctx = _ctx()
    await ext.make_handler(_settings(enabled=False))(ctx)
    assert ctx.headers == {}


@pytest.mark.asyncio
async def test_register_uses_before_mcp_http(monkeypatch):
    monkeypatch.setattr(ext, "load_settings", lambda: _settings())
    calls = []

    class FakeRegistry:
        def register(self, event, handler, priority=0):
            calls.append((str(event), priority))

    extensions = await ext.register_extensions(FakeRegistry())
    assert len(extensions) == 1
    assert extensions[0].metadata.id == "mcp_http_auth"
    assert calls == [("agent_server:before_mcp_http", 10)]


# --- config parsing hardening (review findings) -------------------------------


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off", "", None])
def test_string_falsy_enabled_disables_the_hook(raw):
    """``bool("false") is True``; a quoted false must not keep the hook on."""

    settings = ext.parse_settings({"enabled": raw, "servers": {"gw": {}}})
    assert settings.enabled is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", True])
def test_string_truthy_enabled_keeps_the_hook_on(raw):
    settings = ext.parse_settings({"enabled": raw, "servers": {"gw": {}}})
    assert settings.enabled is True


def test_unknown_enabled_value_fails_closed():
    settings = ext.parse_settings({"enabled": "maybe", "servers": {"gw": {}}})
    assert settings.enabled is False


def test_none_token_prefix_falls_back_to_default():
    """``token_prefix:`` (empty YAML value) must not become the string "None"."""

    settings = ext.parse_settings(
        {"enabled": True, "servers": {"gw": {}}, "token_prefix": None}
    )
    assert settings.token_prefix == ext.DEFAULT_PREFIX


def test_explicit_empty_token_prefix_means_no_prefix():
    settings = ext.parse_settings(
        {"enabled": True, "servers": {"gw": {}}, "token_prefix": ""}
    )
    assert settings.token_prefix == ""


@pytest.mark.parametrize("bad_servers", [["corp-mcp-gw"], "corp-mcp-gw", 3])
def test_non_mapping_servers_fails_closed(bad_servers):
    """A list/str here must not silently degrade to "inject to every server"."""

    settings = ext.parse_settings({"enabled": True, "servers": bad_servers})
    assert settings.enabled is False
    assert settings.servers == {}
