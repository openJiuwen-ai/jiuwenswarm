# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Test plugin preflight, lease ownership and ordinary plugin loading."""

from __future__ import annotations

import asyncio
import json
import threading
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_evolving.trajectory.processor import TrajectorySpanProcessor
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness import agent_observability as observability
from jiuwenswarm.server.runtime import extension_package_manager as equipment
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter import plugin_runtime
from jiuwenswarm.server.runtime.session import session_metadata
from scripts.package_ttse_plugin import package_ttse_plugin


@pytest.mark.asyncio
async def test_completed_timeout_closer_is_retried_without_replacing_live_tasks():
    resource = SimpleNamespace(aclose=AsyncMock())
    binding = SimpleNamespace(runtime=SimpleNamespace(resources=[resource]), close_tasks={})

    async def timed_out():
        raise TimeoutError("previous close budget expired")

    previous = asyncio.create_task(timed_out())
    with pytest.raises(TimeoutError):
        await previous
    binding.close_tasks[id(resource)] = previous

    await plugin_runtime._close_resources(binding, 0.1, 0.1)

    resource.aclose.assert_awaited_once_with(timeout=0.1, grace_timeout=0.1)
    assert binding.close_tasks[id(resource)] is not previous


@pytest.mark.asyncio
async def test_cancelled_closer_propagates_without_starting_another_close():
    resource = SimpleNamespace(aclose=AsyncMock())
    binding = SimpleNamespace(runtime=SimpleNamespace(resources=[resource]), close_tasks={})
    previous = asyncio.create_task(asyncio.sleep(10))
    previous.cancel()
    with pytest.raises(asyncio.CancelledError):
        await previous
    binding.close_tasks[id(resource)] = previous

    with pytest.raises(asyncio.CancelledError):
        await plugin_runtime._close_resources(binding, 0.1, 0.1)

    resource.aclose.assert_not_called()
    assert binding.close_tasks[id(resource)] is previous


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["acquisition", "copying"])
async def test_repeated_prepare_cancellation_reclaims_thread_results(environment, monkeypatch, stage):
    env = environment
    adapter = await env.make_adapter()
    manager = plugin_runtime.get_plugin_runtime_manager(adapter)
    candidate = manager._candidate("ttse", env.package, "2.0.0", {"mode": "code"}, "session-1")
    entered, proceed = threading.Event(), threading.Event()
    snapshots = []
    original_temp = tempfile.TemporaryDirectory
    original_acquire = observability.acquire_plugin_trajectory_demand
    original_copy = plugin_runtime.shutil.copytree

    def snapshot(*, prefix, ignore_cleanup_errors):
        result = original_temp(prefix=prefix, ignore_cleanup_errors=ignore_cleanup_errors)
        snapshots.append(Path(result.name))
        return result

    def acquire(session_id, plugin_id):
        lease = original_acquire(session_id, plugin_id)
        if stage == "acquisition":
            entered.set()
            assert proceed.wait(3)
        return lease

    def copy(source, destination, *, dirs_exist_ok, symlinks):
        entered.set()
        assert proceed.wait(3)
        return original_copy(source, destination, dirs_exist_ok=dirs_exist_ok, symlinks=symlinks)

    monkeypatch.setattr(plugin_runtime, "tempfile", SimpleNamespace(TemporaryDirectory=snapshot))
    monkeypatch.setattr(observability, "acquire_plugin_trajectory_demand", acquire)
    if stage == "copying":
        monkeypatch.setattr(plugin_runtime, "shutil", SimpleNamespace(copytree=copy))
    task = asyncio.create_task(manager.prepare(candidate))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert observability.plugin_trajectory_demand_count() == 1
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert observability.plugin_trajectory_demand_count() == 0
    assert all(not path.exists() for path in snapshots)


@pytest.mark.asyncio
async def test_recovery_retains_dependencies_until_repeated_detach_failure_clears(environment, monkeypatch):
    env = environment
    adapter = await env.make_adapter()
    manager = plugin_runtime.get_plugin_runtime_manager(adapter)
    candidate = manager._candidate("ttse", env.package, "2.0.0", {"mode": "code"}, "session-1")
    binding = await manager.prepare(candidate)
    resource = SimpleNamespace(aclose=AsyncMock())
    binding.runtime.register_resource(resource)
    binding.record = object()
    binding.state = "active"
    adapter._loaded_plugins["ttse"] = (binding.record, "2.0.0")
    slot = plugin_runtime._PluginSlot(manager, "ttse")
    manager.slots["ttse"] = slot
    slot.binding = binding
    slot.disposed = True
    retrying, proceed = asyncio.Event(), asyncio.Event()
    calls = 0

    async def unload(record):
        nonlocal calls
        assert record is binding.record
        calls += 1
        if calls <= 2:
            raise RuntimeError("unload unavailable")
        retrying.set()
        await proceed.wait()

    monkeypatch.setattr(adapter._instance, "unload_extension", unload)
    with pytest.raises(RuntimeError, match="unload unavailable"):
        await manager.remove("ttse")
    recovery = slot.recovery
    try:
        await asyncio.wait_for(retrying.wait(), 3)
        assert observability.plugin_trajectory_demand_count() == 1
        assert Path(binding.snapshot.name).exists()
        assert "ttse" in adapter._loaded_plugins
    finally:
        proceed.set()
        await asyncio.wait_for(recovery, 3)
    resource.aclose.assert_awaited_once()
    assert binding.released
    assert not Path(binding.snapshot.name).exists()
    assert observability.plugin_trajectory_demand_count() == 0
    assert "ttse" not in adapter._loaded_plugins
    assert manager.status("ttse")["state"] == "unmounted"


def _rail(adapter):
    manager = getattr(adapter, "_plugin_runtime_manager", None)
    binding = manager.bindings.get("ttse") if manager is not None else None
    return binding.runtime.resources[0] if binding is not None else None


@pytest.fixture
async def environment(tmp_path, monkeypatch, ttse_plugin_source):
    package = tmp_path / "ttse"
    archive_path = package_ttse_plugin(tmp_path / "ttse.zip", source=ttse_plugin_source)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(package)
    config = {
        "react": {"ttse": {"enabled": False}, "evolution": {"skill_evolution": False}},
        "modes": {"code": {"memory": {"enabled": False}}},
        "agent_observability": {"enabled": False},
        "progressive_tool_enabled": True,
    }
    acquired, released, saved = [], [], []
    for module in (interface_deep, interface_code, observability):
        monkeypatch.setattr(module, "get_config", lambda: config)
    monkeypatch.setattr(observability, "_agent_observability_active", False)
    monkeypatch.setattr(observability, "_force_ever_enabled", False)
    monkeypatch.setattr(observability, "_plugin_trajectory_demands", set())
    monkeypatch.setattr(observability, "acquire_observability", lambda cfg: acquired.append(cfg) or False)
    monkeypatch.setattr(observability, "release_observability", lambda: released.append(True))
    monkeypatch.setattr(observability, "sync_trajectory_runtime", lambda *a, **kw: None)
    monkeypatch.setattr(observability, "shutdown_trajectory_runtime", lambda **kw: True)
    processor = TrajectorySpanProcessor()
    monkeypatch.setattr(
        "openjiuwen.extensions.observability.demand.get_trajectory_span_processor", lambda: processor,
    )
    monkeypatch.setattr(equipment, "resolve_plugin_dir", lambda name: package)
    monkeypatch.setattr(equipment, "resolve_equipment_runtime_id", lambda kind, name: name)
    monkeypatch.setattr(session_metadata, "save_session_equipment", lambda *a, **kw: saved.append(kw))
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *a, **kw: None)
    adapters = []

    async def make_adapter(sid="session-1", profile="code"):
        model = Model(
            model_client_config=ModelClientConfig(
                client_provider="OpenAI", api_key="test", api_base="http://invalid.example",
            ),
            model_config=ModelRequestConfig(model="test-model"),
        )
        adapter_type = interface_code.JiuwenSwarmCodeAdapter if profile == "code" else interface_deep.JiuWenSwarmDeepAdapter
        adapter = adapter_type()
        adapter._parent_session_id = sid
        adapter._agent_workspace_dir = str(tmp_path / "agent-data")
        adapter._workspace_dir = str(tmp_path / "project")
        adapter._config_base_cache = config
        adapter._config_cache = config["react"]
        adapter._model, adapter._default_model_name = model, "test-model"
        adapter._instance = create_deep_agent(
            model=model, auto_create_workspace=False, enable_task_loop=False,
            system_prompt="Test coding agent", tools=[],
            progressive_tool_enabled=True,
        )
        await adapter._instance._ensure_initialized()
        monkeypatch.setattr(adapter, "_marketplace_equipment_gate", lambda params: None)
        monkeypatch.setattr(adapter, "_connector_equipment_gate", lambda params: None)
        monkeypatch.setattr(adapter, "_has_conflicting_inflight_turn", lambda sid: False)
        adapters.append(adapter)
        return adapter

    async def select(adapter, plugins=None, **extra):
        params = {"mode": "code", **extra}
        if plugins is not None:
            params["plugin_names"] = plugins
        request = SimpleNamespace(
            params=params, session_id=adapter._parent_session_id,
            request_id="test-request", channel_id="web", metadata={},
        )
        return await adapter._ensure_chat_extensions(request)

    yield SimpleNamespace(
        make_adapter=make_adapter, select=select, package=package, config=config,
        acquired=acquired, released=released, saved=saved, processor=processor,
    )
    for adapter in adapters:
        manager = getattr(adapter, "_plugin_runtime_manager", None)
        if manager is not None:
            await manager.close()
        if adapter._ttse_rail is not None:
            await adapter._ttse_rail.aclose()
            await adapter._instance.unregister_rail(adapter._ttse_rail)
            adapter._ttse_rail = None
    assert observability.plugin_trajectory_demand_count() == 0


@pytest.mark.asyncio
async def test_provider_failure_releases_failed_construction_requirement(environment, monkeypatch):
    env = environment
    adapter = await env.make_adapter()

    def fail(_config):
        raise OSError("provider unavailable")

    monkeypatch.setattr(observability, "acquire_observability", fail)
    error = await env.select(adapter, ["ttse"])
    assert error is not None and not error.ok
    assert observability.plugin_trajectory_demand_count() == 0
    assert _rail(adapter) is None


@pytest.mark.asyncio
async def test_cancelled_acquisition_releases_lease_created_by_worker_thread(environment, monkeypatch):
    env = environment
    adapter = await env.make_adapter()
    entered, release = threading.Event(), threading.Event()
    original = observability.acquire_plugin_trajectory_demand

    def acquire(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(observability, "acquire_plugin_trajectory_demand", acquire)
    task = asyncio.create_task(env.select(adapter, ["ttse"]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert observability.plugin_trajectory_demand_count() == 0
    assert _rail(adapter) is None


@pytest.mark.asyncio
async def test_registry_is_thread_safe_and_release_is_idempotent(environment):
    def acquire(index):
        return observability.acquire_plugin_trajectory_demand(f"session-{index}", "ttse")

    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(acquire, range(32)))
        assert observability.plugin_trajectory_demand_count() == 32
        list(pool.map(lambda lease: lease.release(), leases + leases))
    assert observability.plugin_trajectory_demand_count() == 0
    assert len(environment.released) == 1


@pytest.mark.asyncio
async def test_nonfresh_equipment_change_does_not_prepare_runtime(environment, monkeypatch):
    adapter = await environment.make_adapter()
    monkeypatch.setattr(adapter, "_has_conflicting_inflight_turn", lambda sid: True)
    error = await environment.select(adapter, ["ttse"])
    assert error is not None and "non-fresh turn" in error.payload["error"]
    assert not environment.acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"mode": "agent.work.normal"},
    {"mode": "agent.work.plan"},
    {"mode": "code", "team": True},
    {"mode": "code", "sub_mode": "team"},
    {"mode": "code", "submode": "team"},
])
async def test_non_main_code_wire_modes_fail_before_acquiring_services(environment, params):
    env = environment
    adapter = await env.make_adapter()
    error = await env.select(adapter, ["ttse"], **params)
    assert error is not None and not error.ok
    assert not env.acquired
    assert observability.plugin_trajectory_demand_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "team.code.normal", "team.code.plan", "code.team", "team.plan.code",
])
async def test_team_modes_skip_equipment_and_runtime_rejects_direct_mount(environment, mode):
    env = environment
    adapter = await env.make_adapter()
    assert await env.select(adapter, ["ttse"], mode=mode) is None
    manager = plugin_runtime.get_plugin_runtime_manager(adapter)
    with pytest.raises(ValueError, match="main agent profile"):
        async with manager.transition({"mode": mode, "plugin_names": ["ttse"]}, "session-1"):
            pytest.fail("Team mode must not enter the mount transaction")
    assert not env.acquired
    assert _rail(adapter) is None


@pytest.mark.asyncio
async def test_scope_and_contract_fail_before_mounting(environment):
    env = environment
    adapter = await env.make_adapter()
    error = await env.select(adapter, ["ttse"], sub_mode="team")
    assert error is not None and not error.ok
    adapter._instance._jiuwenswarm_code_team_member = True
    error = await env.select(adapter, ["ttse"])
    assert error is not None and not error.ok
    adapter._instance._jiuwenswarm_code_team_member = False
    manifest = env.package / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["metadata"]["plugin_runtime"]["contract_version"] = 99
    manifest.write_text(json.dumps(data), encoding="utf-8")
    error = await env.select(adapter, ["ttse"])
    assert error is not None and not error.ok
    assert not env.acquired


def _write_other_plugin(path, name, *, capabilities=None, requires=None, ordinary=False):
    """A separate business plugin requiring no new host implementation."""
    path.mkdir()
    manifest = {
        "package_type": "plugin", "id": name, "name": name, "version": "1.0.0",
        "rails": [{"file": "rail.py", "class": f"{name.title()}Rail"}],
    }
    if not ordinary:
        manifest["metadata"] = {"plugin_runtime": {
            "contract_version": 1, "profiles": ["code"], "role_scope": "main",
            "requires": requires or [], "capabilities": capabilities or [],
        }}
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    code = '''from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.prompts.builder import PromptSection

class PLUGIN_CLASS(DeepAgentRail):
    def __init__(self):
        super().__init__()
        self.closed = False

    @classmethod
    def prepare_plugin_context(cls, runtime):
        if runtime.options.get("invalid"):
            raise ValueError("business options are invalid")
        return dict(runtime.options)

    @classmethod
    def from_plugin_context(cls, *, params, context):
        runtime = context.extras["plugin_runtime"]
        rail = cls()
        rail.runtime = runtime
        if runtime.options.get("fail_init"):
            rail.fail_init = True
        return runtime.register_resource(rail)

    def init(self, agent):
        agent.system_prompt_builder.add_section(PromptSection(name="PLUGIN_ID", content={"cn": "policy", "en": "policy"}))
        if getattr(self, "fail_init", False):
            raise RuntimeError("business init failed")

    def uninit(self, agent):
        agent.system_prompt_builder.remove_section("PLUGIN_ID")

    async def aclose(self, *, timeout, grace_timeout):
        self.closed = True
'''.replace("PLUGIN_CLASS", f"{name.title()}Rail").replace("PLUGIN_ID", name)
    (path / "rail.py").write_text(code, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_capability_conflict_and_unknown_service_fail_before_any_acquisition(environment, tmp_path, monkeypatch):
    env = environment
    notes = _write_other_plugin(tmp_path / "notes", "notes", capabilities=["ttse"])
    monkeypatch.setattr(equipment, "resolve_plugin_dir", {"ttse": env.package, "notes": notes}.__getitem__)
    adapter = await env.make_adapter()
    error = await env.select(adapter, ["ttse", "notes"])
    assert error is not None and "capability" in error.payload["error"]
    assert not env.acquired
    manifest = notes / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["metadata"]["plugin_runtime"]["requires"] = ["unknown_service"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    error = await env.select(adapter, ["notes"])
    assert error is not None and "unsupported host service" in error.payload["error"]
    assert not env.acquired


@pytest.mark.asyncio
async def test_ordinary_zero_argument_plugin_needs_no_runtime_contract(environment, tmp_path, monkeypatch):
    env = environment
    plain = _write_other_plugin(tmp_path / "plain", "plain", ordinary=True)
    # A conventional plugin has only a zero-argument constructor and lifecycle.
    (plain / "rail.py").write_text('''from openjiuwen.harness.rails.base import DeepAgentRail
class PlainRail(DeepAgentRail):
    pass
''', encoding="utf-8")
    monkeypatch.setattr(equipment, "resolve_plugin_dir", lambda name: plain)
    adapter = await env.make_adapter()
    assert await env.select(adapter, ["plain"]) is None
    assert not adapter._plugin_runtime_manager.bindings
    assert not env.acquired
    assert adapter._instance.find_rail_by_name("PlainRail") is not None
    assert await env.select(adapter, []) is None
    assert adapter._instance.find_rail_by_name("PlainRail") is None


