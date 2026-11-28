# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inactive harness packages do not require the optional evolving SDK."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("ledger_exists", [False, True])
def test_plain_execution_and_inactive_packages_do_not_import_rsi(tmp_path, ledger_exists):
    root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    packages_file = data_dir / "auto-harness" / "harness-packages.json"
    if ledger_exists:
        packages_file.parent.mkdir(parents=True)
        packages_file.write_text(json.dumps({
            "active_package_ids": [],
            "packages": [{"id": "inactive", "config_path": "unused.yaml"}],
        }), encoding="utf-8")
    script = f"""
import asyncio, importlib.abc, sys
from pathlib import Path
sys.path.insert(0, {str(root)!r})
blocked = ('openjiuwen.rsi', 'jiuwenswarm.agents.harness.common.auto_harness.service')
attempted = []
class RejectEvolvingSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            attempted.append(fullname)
            raise AssertionError('optional evolving SDK required: ' + fullname)
sys.meta_path.insert(0, RejectEvolvingSdk())
from jiuwenswarm.agents.harness.common.auto_harness.state import get_harness_packages_file
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
assert get_harness_packages_file() == Path({str(packages_file)!r})
adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
adapter._instance = object()
assert adapter._get_active_package_config_paths() == []
assert asyncio.run(adapter._load_active_packages()) == []
assert not attempted, attempted
assert not any(name == 'openjiuwen.rsi' or name.startswith('openjiuwen.rsi.') for name in sys.modules)
print('AUTO_HARNESS_IMPORT_BOUNDARY_OK')
"""
    env = dict(
        os.environ,
        JIUWENSWARM_DATA_DIR=str(data_dir),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUTO_HARNESS_IMPORT_BOUNDARY_OK" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("config_text, allowed", [
    ("id: safe\ntools: []\nrails: []\nskills: []\n", True),
    ("id: unsafe\nmcps: [{command: python}]\n", False),
    ("id: unsafe\ntools: [{file: ../outside.py, class: OutsideTool}]\n", False),
])
async def test_explicit_package_activation_keeps_real_validation(tmp_path, config_text, allowed):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    package_dir = tmp_path / "package"
    package_dir.mkdir()
    config_file = package_dir / "harness_config.yaml"
    config_file.write_text(config_text, encoding="utf-8")
    runtime = SimpleNamespace(load_harness_config=AsyncMock(return_value=["tool:sample"]))
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = runtime

    result = await adapter.apply_package_change("activate", str(config_file))

    if allowed:
        assert result == ["tool:sample"]
        runtime.load_harness_config.assert_awaited_once_with(str(config_file))
    else:
        assert result is None
        runtime.load_harness_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_packages_use_current_workspace_and_keep_validation(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.auto_harness.state import get_harness_packages_file
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    safe_config = tmp_path / "safe.yaml"
    safe_config.write_text("id: safe\ntools: []\n", encoding="utf-8")
    unsafe_config = tmp_path / "unsafe.yaml"
    unsafe_config.write_text("id: unsafe\nmcps: [{command: python}]\n", encoding="utf-8")
    packages_file = get_harness_packages_file()
    assert packages_file == tmp_path / "auto-harness" / "harness-packages.json"
    packages_file.parent.mkdir(parents=True)
    packages_file.write_text(json.dumps({
        "active_package_ids": ["safe", "unsafe"],
        "packages": [
            {"id": "safe", "config_path": str(safe_config)},
            {"id": "unsafe", "config_path": str(unsafe_config)},
        ],
    }), encoding="utf-8")
    runtime = SimpleNamespace(
        unload_harness_config=AsyncMock(),
        load_harness_config=AsyncMock(return_value=["tool:safe"]),
    )
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = runtime

    assert await adapter._load_active_packages() == ["tool:safe"]
    runtime.load_harness_config.assert_awaited_once_with(str(safe_config))
    assert runtime.unload_harness_config.await_count == 2
    assert json.loads(packages_file.read_text(encoding="utf-8"))["active_package_ids"] == ["safe", "unsafe"]
