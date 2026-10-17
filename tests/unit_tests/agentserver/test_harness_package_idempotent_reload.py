# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for idempotent harness package reload (先卸后装).

Covers the fix for the "tools not loaded after running a while" bug:

1. ``_load_active_packages`` must unload every active package *before*
   re-binding. openjiuwen's ``apply_extension_hot`` raises on a same-name
   resource already present and rolls back the whole batch; without the
   preceding unload, reloading onto an agent that already carries those tool
   names would fail silently (the exception is swallowed by
   ``apply_package_change``).

2. ``reload_agent_config`` must re-bind active packages after
   ``DeepAgent.configure()`` reconciles ``config.tools`` — configure drops
   harness-injected tools (they live in ``deep_config.tools`` via
   ``_bind_tool``, not in the config.yaml-driven ``tool_cards``) as stale, so
   without the re-bind every config/MCP/model save strips harness tools.
"""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """A bare adapter with only the state ``_load_active_packages`` touches."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace()
    return adapter


def _stub_instance(adapter: JiuWenSwarmDeepAdapter) -> list[str]:
    """Wire ``_instance`` load/unload recorders; return the call trace list."""
    calls: list[str] = []

    async def _unload(config_path: str) -> list[str]:
        calls.append(f"unload:{config_path}")
        return []

    async def _load(config_path: str) -> list[str]:
        calls.append(f"load:{config_path}")
        return ["tool:generate_docx"]

    adapter._instance.unload_harness_config = _unload
    adapter._instance.load_harness_config = _load
    return calls


@pytest.mark.asyncio
async def test_load_active_packages_unloads_before_load(monkeypatch):
    """Reload must clear existing bindings first so re-bind does not trip
    "already bound" (which would roll back the batch and be swallowed)."""
    adapter = _make_adapter()
    calls = _stub_instance(adapter)

    paths = ["/pkgs/a/harness_config.yaml", "/pkgs/b/harness_config.yaml"]
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_active_package_config_paths",
        staticmethod(lambda: list(paths)),
    )

    loaded = await adapter._load_active_packages()

    # Every active path is unloaded once, then every path loaded once, in order,
    # and unload runs strictly before any load.
    expected = [f"unload:{p}" for p in paths] + [f"load:{p}" for p in paths]
    assert calls == expected, f"unload-before-load order broken: {calls}"
    # Both packages' resources are accumulated into the result.
    assert loaded == ["tool:generate_docx", "tool:generate_docx"]


@pytest.mark.asyncio
async def test_load_active_packages_unload_failures_do_not_block_load(monkeypatch):
    """A failing unload (e.g. manifest already gone) must not skip the load —
    openjiuwen treats a missing ledger record as a no-op, and the load still
    needs to run to restore tools."""
    adapter = _make_adapter()
    calls: list[str] = []

    async def _unload(config_path: str) -> list[str]:
        calls.append(f"unload:{config_path}")
        raise RuntimeError("ledger miss")

    async def _load(config_path: str) -> list[str]:
        calls.append(f"load:{config_path}")
        return ["tool:generate_docx"]

    adapter._instance.unload_harness_config = _unload
    adapter._instance.load_harness_config = _load

    paths = ["/pkgs/a/harness_config.yaml"]
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_active_package_config_paths",
        staticmethod(lambda: list(paths)),
    )

    # Should not raise despite unload erroring.
    loaded = await adapter._load_active_packages()
    assert calls == ["unload:/pkgs/a/harness_config.yaml", "load:/pkgs/a/harness_config.yaml"]
    assert loaded == ["tool:generate_docx"]


@pytest.mark.asyncio
async def test_load_active_packages_load_failure_is_swallowed_per_package(monkeypatch):
    """One package failing to load must not abort the rest — each is independent."""
    adapter = _make_adapter()
    calls: list[str] = []

    async def _unload(config_path: str) -> list[str]:
        return []

    async def _load(config_path: str) -> list[str]:
        calls.append(f"load:{config_path}")
        if "failing" in config_path:
            raise ValueError("Tool already bound: generate_docx")
        return ["tool:generate_docx"]

    adapter._instance.unload_harness_config = _unload
    adapter._instance.load_harness_config = _load

    paths = ["/pkgs/ok/harness_config.yaml", "/pkgs/failing/harness_config.yaml"]
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_active_package_config_paths",
        staticmethod(lambda: list(paths)),
    )

    # Should not raise despite one load failing.
    loaded = await adapter._load_active_packages()
    assert calls == ["load:/pkgs/ok/harness_config.yaml", "load:/pkgs/failing/harness_config.yaml"]
    # The successful package's resources still come through.
    assert loaded == ["tool:generate_docx"]


@pytest.mark.asyncio
async def test_load_active_packages_no_instance_is_noop():
    """No instance (root adapter never built) -> empty, no exception."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    loaded = await adapter._load_active_packages()
    assert loaded == []


@pytest.mark.asyncio
async def test_load_active_packages_skips_mcps_bearing_package(tmp_path, monkeypatch):
    """An active package whose harness_config.yaml declares ``mcps`` must be
    skipped on cold-start reload — never fed to load_harness_config, which
    would spawn its declared subprocess. Covers packages imported before the
    import-time guard existed or placed on disk directly."""
    adapter = _make_adapter()
    calls: list[str] = []

    async def _unload(config_path: str) -> list[str]:
        return []

    async def _load(config_path: str) -> list[str]:
        calls.append(f"load:{config_path}")
        return ["tool:generate_docx"]

    adapter._instance.unload_harness_config = _unload
    adapter._instance.load_harness_config = _load

    # mcps-bearing config on disk (the real file the guard reads)
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_cfg = bad_dir / "harness_config.yaml"
    bad_cfg.write_text(
        "id: poc\nmcps:\n  - {server_name: poc, command: python, args: ['-c', 'x']}\n",
        encoding="utf-8",
    )
    # A clean config alongside it
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    ok_cfg = ok_dir / "harness_config.yaml"
    ok_cfg.write_text("id: ok\ntools: []\n", encoding="utf-8")

    paths = [str(bad_cfg), str(ok_cfg)]
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_active_package_config_paths",
        staticmethod(lambda: list(paths)),
    )

    loaded = await adapter._load_active_packages()

    # Only the clean package is loaded; the mcps one is skipped (no load call).
    assert calls == [f"load:{ok_cfg}"]
    assert loaded == ["tool:generate_docx"]


@pytest.mark.asyncio
async def test_unload_active_packages_no_instance_is_noop(monkeypatch):
    """No instance -> _unload_active_packages returns without touching config."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    # Pass an explicit path list; with no instance this must short-circuit
    # before reading paths (which would otherwise touch the monkeypatch).
    await adapter._unload_active_packages(["/pkgs/a/harness_config.yaml"])


@pytest.mark.asyncio
async def test_unload_active_packages_accepts_explicit_paths():
    """Callers may pass the path list to avoid a second JSON read during reload."""
    adapter = _make_adapter()
    calls: list[str] = []

    async def _unload(config_path: str) -> list[str]:
        calls.append(config_path)
        return []

    adapter._instance.unload_harness_config = _unload

    paths = ["/pkgs/a/harness_config.yaml", "/pkgs/b/harness_config.yaml"]
    await adapter._unload_active_packages(paths)

    assert calls == paths
