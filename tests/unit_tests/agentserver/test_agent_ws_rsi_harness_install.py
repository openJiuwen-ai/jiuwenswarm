# -*- coding: utf-8 -*-
"""AgentServer composition and RSI Harness baseline tests."""

import json
import pytest

from jiuwenswarm.agents.harness.common.rsi.harness_activation import RsiHarnessActivationStore
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def _bare_server(manager):
    server = object.__new__(AgentWebSocketServer)
    server._rsi_handlers = None
    server._rsi_harness_provider = None
    server._agent_manager = manager
    return server


def test_rsi_context_binds_installer_to_agent_manager(monkeypatch, tmp_path):
    class FakeManager:
        pass

    manager = FakeManager()
    monkeypatch.setenv("RSI_PROVIDER_MODE", "mock")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path,
    )

    handlers = _bare_server(manager)._get_rsi_handlers()

    assert handlers.context.harness_installer.agent_manager is manager


def test_rsi_active_harness_precedes_generic_registry(monkeypatch, tmp_path):
    runtime_path = (
        tmp_path
        / "rsi"
        / "tasks"
        / "rsi-task"
        / "harness"
        / "versions"
        / "install-a"
        / "validation_harness"
    )
    runtime_path.mkdir(parents=True)
    store = RsiHarnessActivationStore(tmp_path / "rsi" / "tasks")
    store.commit(
        {
            "installation_id": "install-a",
            "task_id": "rsi-task",
            "runtime_path": str(runtime_path),
            "sha256": "a" * 64,
        }
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path,
    )

    server = _bare_server(object())

    assert server._rsi_harness_refs_provider() == str(runtime_path.resolve())


def test_rsi_initial_refs_are_a_trusted_input_fallback(monkeypatch, tmp_path):
    initial_refs = tmp_path / "rsi" / "harness" / "initial_harness_refs.yaml"
    initial_refs.parent.mkdir(parents=True)
    initial_refs.write_text("version: 1\nharness_refs: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path,
    )

    server = _bare_server(object())

    assert server._rsi_harness_refs_provider() == str(initial_refs.resolve())


def test_rsi_missing_legacy_registry_uses_native_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.auto_harness.service._HARNESS_PACKAGES_FILE",
        tmp_path / "missing.json",
    )
    result = _bare_server(object())._rsi_harness_refs_provider()
    assert result and result.endswith("harness_config.yaml")


def test_rsi_explicit_plugin_selection_uses_chat_registry(monkeypatch, tmp_path):
    package = tmp_path / "selected-plugin"
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps({"id": "selected-plugin", "package_type": "plugin"}))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.is_plugin_allowed", lambda name: name == package.name,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.resolve_plugin_dir", lambda name: package,
    )
    monkeypatch.setattr(
        RsiHarnessActivationStore, "resolve_active_runtime_path",
        lambda self: str(tmp_path / "different-active-harness"),
    )
    assert _bare_server(object())._rsi_harness_refs_provider({"package_id": package.name}) == str(package.resolve())


def test_rsi_invalid_plugin_does_not_fall_back_to_other_harness(monkeypatch):
    from jiuwenswarm.agents.harness.common.rsi.errors import RsiInvalidHarness
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.is_plugin_allowed", lambda name: False)
    with pytest.raises(RsiInvalidHarness, match="not installed"):
        _bare_server(object())._rsi_harness_refs_provider({"package_id": "missing"})


def test_rsi_plugin_with_unavailable_mcp_fails_before_training(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.rsi.errors import RsiInvalidHarness
    package = tmp_path / "mcp-plugin"
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps({
        "id": "mcp-plugin", "package_type": "plugin", "mcps": [{"name": "external"}],
    }))
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.is_plugin_allowed", lambda name: True)
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.resolve_plugin_dir", lambda name: package)
    with pytest.raises(RsiInvalidHarness, match="MCP"):
        _bare_server(object())._rsi_harness_refs_provider({"package_id": "mcp-plugin"})
