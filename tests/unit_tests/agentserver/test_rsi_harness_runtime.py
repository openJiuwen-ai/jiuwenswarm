# -*- coding: utf-8 -*-
"""Dedicated RSI Harness runtime hot-load tests."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager


class FakeDeepAgent:
    def __init__(self):
        self.load_plugin_calls = []
        self.load_harness_config_calls = []
        self.unload_extension_calls = []
        self.fail_paths = set()

    async def load_plugin(self, path):
        self.load_plugin_calls.append(path)
        if path in self.fail_paths:
            raise RuntimeError(f"Tool already bound: {path}")
        return SimpleNamespace(load_id=f"load-{len(self.load_plugin_calls)}", refs=[f"tool:{path}"])

    async def unload_extension(self, record):
        self.unload_extension_calls.append(record)
        return [f"unloaded:{record.load_id}"]

    async def load_harness_config(self, path):  # pragma: no cover - guard against accidental reuse
        self.load_harness_config_calls.append(path)
        raise AssertionError("RSI path must not call deprecated load_harness_config")


def _make_adapter(instance):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = instance
    adapter._session_adapters = {}
    adapter._is_session_scoped_adapter = False
    adapter._rsi_harness_install_id = None
    adapter._rsi_harness_config_path = None
    adapter._rsi_harness_load_record = None
    return adapter


@pytest.mark.asyncio
async def test_rsi_adapter_uses_load_plugin_record_not_deprecated_config_api():
    instance = FakeDeepAgent()
    adapter = _make_adapter(instance)

    result = await adapter.apply_rsi_harness_install(
        "activate",
        config_path="C:/rsi/validation_harness",
        installation_id="install-a",
    )

    assert result["status"] == "ACTIVE"
    assert instance.load_plugin_calls == ["C:/rsi/validation_harness"]
    assert instance.load_harness_config_calls == []
    assert adapter._rsi_harness_load_record is not None


@pytest.mark.asyncio
async def test_new_session_restores_rsi_installation_from_tasks_root(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.rsi.harness_activation import RsiHarnessActivationStore
    runtime = tmp_path / "rsi" / "tasks" / "task" / "harness" / "versions" / "v1" / "plugin"
    runtime.mkdir(parents=True)
    RsiHarnessActivationStore(tmp_path / "rsi" / "tasks").commit({
        "installation_id": "v1", "runtime_path": str(runtime), "sha256": "a" * 64,
    })
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    instance = FakeDeepAgent()
    adapter = _make_adapter(instance)
    result = await adapter._load_rsi_active_harness()
    assert result["status"] == "ACTIVE"
    assert instance.load_plugin_calls == [str(runtime)]


@pytest.mark.asyncio
async def test_rsi_adapter_keeps_old_record_when_new_load_fails():
    instance = FakeDeepAgent()
    adapter = _make_adapter(instance)
    await adapter.apply_rsi_harness_install(
        "activate", config_path="C:/rsi/old", installation_id="old"
    )
    instance.fail_paths.add("C:/rsi/new")

    with pytest.raises(RuntimeError, match="Tool already bound"):
        await adapter.apply_rsi_harness_install(
            "activate", config_path="C:/rsi/new", installation_id="new"
        )

    assert adapter._rsi_harness_install_id == "old"
    assert adapter._rsi_harness_config_path == "C:/rsi/old"
    assert instance.load_plugin_calls == ["C:/rsi/old", "C:/rsi/new", "C:/rsi/old"]


@pytest.mark.asyncio
async def test_rsi_adapter_fans_out_to_session_adapters():
    root_instance = FakeDeepAgent()
    child_instance = FakeDeepAgent()
    root = _make_adapter(root_instance)
    child = _make_adapter(child_instance)
    child._is_session_scoped_adapter = True
    root._session_adapters = {"session-a": child}

    result = await root.apply_rsi_harness_install(
        "activate", config_path="C:/rsi/pkg", installation_id="install-a"
    )

    assert result["attempted"] == 2
    assert root_instance.load_plugin_calls == ["C:/rsi/pkg"]
    assert child_instance.load_plugin_calls == ["C:/rsi/pkg"]


class FakeFacade:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.ensure_calls = 0
        self.calls = []

    async def ensure_instance(self):
        self.ensure_calls += 1
        return object()

    async def apply_rsi_harness_install(self, operation, *, config_path, installation_id):
        self.calls.append((operation, config_path, installation_id))
        if self.fail and operation == "activate":
            raise RuntimeError("resource conflict")
        return {"status": "ACTIVE", "resources": []}


@pytest.mark.asyncio
async def test_agent_manager_rsi_broadcast_targets_agent_and_code_only():
    manager = AgentManager()
    agent = FakeFacade()
    code = FakeFacade()
    team = FakeFacade()
    manager.agents = {"web": {"agent::": agent, "code::": code, "team::": team}}

    result = await manager.broadcast_rsi_harness_change(
        old_installation=None,
        new_installation={
            "installation_id": "install-a",
            "runtime_path": "C:/rsi/pkg",
        },
    )

    assert result == {"attempted": 2, "succeeded": 2, "failed": []}
    assert team.calls == []
    assert all(call[0] == "activate" for call in agent.calls + code.calls)


@pytest.mark.asyncio
async def test_agent_manager_can_deactivate_new_version_when_no_old_active_exists():
    manager = AgentManager()
    agent = FakeFacade()
    manager.agents = {"web": {"agent::": agent}}

    result = await manager.broadcast_rsi_harness_change(
        old_installation={
            "installation_id": "install-new",
            "runtime_path": "C:/rsi/new",
        },
        new_installation=None,
    )

    assert result == {"attempted": 1, "succeeded": 1, "failed": []}
    assert agent.calls == [("deactivate", "C:/rsi/new", "install-new")]
