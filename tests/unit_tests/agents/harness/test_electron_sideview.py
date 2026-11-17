# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Discovery ownership and lazy, per-panel SDK bindings."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common import electron_sideview as sideview


@dataclass(frozen=True)
class _FakeMcpConfig:
    params: dict
    server_id: str = "original"
    server_name: str = "original"

    def model_copy(self, *, update: dict) -> "_FakeMcpConfig":
        return _FakeMcpConfig(**{**vars(self), **update})


@dataclass(frozen=True)
class _FakeSettings:
    mcp_cfg: _FakeMcpConfig


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("PLAYWRIGHT_MCP_", "BROWSER_", "JIUWENSWARM_ELECTRON")):
            monkeypatch.delenv(key)
    monkeypatch.delenv(sideview.FORCE_MANAGED_ENV, raising=False)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sideview, "_discovery_original", {})
    monkeypatch.setattr(sideview, "_discovery_applied", {})
    monkeypatch.setattr(sideview, "_pid_alive", lambda _pid: True)
    yield
    sideview._restore_discovery_env()


def _discovery(*, age=0):
    payload = {
        "pid": 123,
        "written_at": (time.time() - age) * 1000,
        "cdp_endpoint": "http://127.0.0.1:41001",
        "target_resolver": "http://127.0.0.1:41002",
        "mcp_command": "node",
        "mcp_args": ["target_mcp_wrapper.cjs"],
        "env_json": {"PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN": "test-token"},
    }
    target = sideview.electron_discovery_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_ordinary_backend_does_not_discover_electron():
    _discovery()
    assert not sideview.apply_electron_discovery_browser_env()
    assert sideview.electron_target_resolver_base() == ""
    assert "BROWSER_DRIVER" not in os.environ


@pytest.mark.parametrize("driver", ["managed", "extension"])
def test_discovery_respects_explicit_driver(monkeypatch, driver):
    _discovery()
    monkeypatch.setenv(sideview.DISCOVERY_OPT_IN_ENV, "1")
    monkeypatch.setenv("BROWSER_DRIVER", driver)
    assert not sideview.apply_electron_discovery_browser_env()
    assert os.environ["BROWSER_DRIVER"] == driver


@pytest.mark.parametrize("expired", ["missing", "old", "dead", "disabled", "force_managed"])
def test_stale_discovery_restores_original_configuration(monkeypatch, expired):
    target = _discovery()
    monkeypatch.setenv(sideview.DISCOVERY_OPT_IN_ENV, "1")
    monkeypatch.setenv("PLAYWRIGHT_MCP_COMMAND", "original-command")
    assert sideview.apply_electron_discovery_browser_env()
    assert sideview.apply_electron_discovery_browser_env()
    assert os.environ["BROWSER_DRIVER"] == "remote"
    if expired == "missing":
        target.unlink()
    elif expired == "old":
        _discovery(age=60)
    elif expired == "dead":
        monkeypatch.setattr(sideview, "_pid_alive", lambda _pid: False)
    elif expired == "disabled":
        monkeypatch.setenv(sideview.DISCOVERY_OPT_IN_ENV, "0")
    else:
        monkeypatch.setenv(sideview.FORCE_MANAGED_ENV, "1")
    assert sideview.electron_target_resolver_base() == ""
    assert "BROWSER_DRIVER" not in os.environ
    assert "PLAYWRIGHT_MCP_ENV_JSON" not in os.environ
    assert os.environ["PLAYWRIGHT_MCP_COMMAND"] == "original-command"


def test_cleanup_does_not_clobber_user_changes(monkeypatch):
    target = _discovery()
    monkeypatch.setenv(sideview.DISCOVERY_OPT_IN_ENV, "1")
    assert sideview.apply_electron_discovery_browser_env()
    monkeypatch.setenv("BROWSER_DRIVER", "managed")
    monkeypatch.setenv("PLAYWRIGHT_MCP_COMMAND", "new-command")
    target.unlink()
    assert not sideview.electron_browser_selected()
    assert os.environ["BROWSER_DRIVER"] == "managed"
    assert os.environ["PLAYWRIGHT_MCP_COMMAND"] == "new-command"
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" not in os.environ


def test_spawned_or_explicit_resolver_is_not_overwritten(monkeypatch):
    _discovery()
    monkeypatch.setenv(sideview.DISCOVERY_OPT_IN_ENV, "1")
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:50000")
    assert sideview.electron_target_resolver_base() == "http://127.0.0.1:50000"
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" not in os.environ


def test_force_managed_disables_inherited_electron_selection(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:50000")
    monkeypatch.setenv("JIUWENSWARM_ELECTRON", "1")
    monkeypatch.setenv(sideview.FORCE_MANAGED_ENV, "1")
    assert not sideview.electron_browser_selected()


def test_binding_is_lazy_immutable_and_unique_per_member_and_conversation(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124")
    settings = _FakeSettings(_FakeMcpConfig({"command": "node", "env": {
        "PLAYWRIGHT_MCP_TARGET_ID": "stale-target",
        "PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN": "test-token",
    }}))
    identities = [("one", ""), ("two", ""), ("one", "member-a"), ("one", "member-b")]
    results = [sideview.apply_session_sideview_target(settings, sid, member_id=member) for sid, member in identities]
    assert len({result.mcp_cfg.server_id for result in results}) == 4
    for result, (sid, member) in zip(results, identities):
        assert result.mcp_cfg.server_name == "playwright-official"
        env = result.mcp_cfg.params["env"]
        assert "PLAYWRIGHT_MCP_TARGET_ID" not in env
        assert env["PLAYWRIGHT_MCP_SESSION_ID"] == sid
        assert env["PLAYWRIGHT_MCP_MEMBER_ID"] == member
        assert env["PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN"] == "test-token"
    assert settings.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_TARGET_ID"] == "stale-target"
    assert sideview.apply_session_sideview_target(results[0], "one") == results[0]


def test_external_browser_settings_are_untouched():
    settings = _FakeSettings(_FakeMcpConfig({"command": "npx"}))
    assert sideview.apply_session_sideview_target(settings, "sess_a") is settings


@pytest.mark.parametrize("mode", ["agent", "code"])
@pytest.mark.parametrize("chrome_path", ["", "/configured/chrome"])
def test_single_agent_adapters_bind_browser_to_their_own_conversation(monkeypatch, mode, chrome_path):
    from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep

    module = interface_deep if mode == "agent" else interface_code
    adapter_type = module.JiuWenSwarmDeepAdapter if mode == "agent" else module.JiuwenSwarmCodeAdapter
    monkeypatch.setenv("BROWSER_DRIVER", "remote")
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124")
    original = _FakeSettings(_FakeMcpConfig({"command": "node"}))
    monkeypatch.setattr(
        module, "build_browser_agent_config",
        lambda *args, **kwargs: SimpleNamespace(factory_kwargs={"settings": original}),
    )
    monkeypatch.setattr(adapter_type, "_browser_runtime_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(adapter_type, "_resolve_runtime_language", lambda self: "en")
    monkeypatch.setattr(adapter_type, "_prepare_browser_runtime_security", lambda *args: None)
    monkeypatch.setattr(adapter_type, "_sync_mcp_credentials_environment", lambda *args: None)
    monkeypatch.setattr(interface_deep, "_load_custom_subagents", lambda **kwargs: [])
    parent = adapter_type()
    options = {"subagents": {
        "statusline-setup": {"enabled": False}, "explore_agent": {"enabled": False},
        "plan_agent": {"enabled": False}, "browser_agent": {"enabled": True},
    }}
    configs = []
    for sid in ("single-one", "single-two"):
        child = parent._new_session_scoped_adapter(sid)
        specs, _ = child._build_configured_subagents(object(), options, {"browser": {"chrome_path": chrome_path}})
        assert len(specs) == 1
        config = specs[0].factory_kwargs["settings"].mcp_cfg
        assert config.params["env"]["PLAYWRIGHT_MCP_SESSION_ID"] == sid
        assert config.params["env"]["PLAYWRIGHT_MCP_MEMBER_ID"] == ""
        configs.append(config)
    assert configs[0].server_id != configs[1].server_id
    assert original.mcp_cfg.params == {"command": "node"}


def test_swarm_provider_binds_each_members_settings(monkeypatch):
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers import code_subagents

    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124")
    original = _FakeSettings(_FakeMcpConfig({"command": "node"}))
    monkeypatch.setattr(
        code_subagents, "build_browser_agent_config",
        lambda *args, **kwargs: SimpleNamespace(factory_kwargs={"settings": original}),
    )
    configs = []
    for member in ("alice", "bob"):
        ctx = SwarmBuildContext(mode="code.team", role="teammate", member_name=member, session_id="one", config={})
        ctx.extras["_parent_model"] = object()
        spec = code_subagents.build_swarm_browser_agent({}, ctx)
        config = spec.factory_kwargs["settings"].mcp_cfg
        assert spec.factory_kwargs["auto_create_workspace"] is False
        assert config.params["env"]["PLAYWRIGHT_MCP_MEMBER_ID"] == member
        assert config.params["env"]["PLAYWRIGHT_MCP_PANEL_LABEL"] == member
        assert config.params["env"]["PLAYWRIGHT_MCP_SESSION_ID"] == "one"
        configs.append(config)
    assert configs[0].server_id != configs[1].server_id
    assert original.mcp_cfg.params == {"command": "node"}
