# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single-agent expert selection reloads on version drift; the version comes from openjiuwen's card."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime import extension_package_manager as equipment
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


def _write_manifest(package_dir: Path, version: str | None) -> None:
    manifest = {"package_type": "agent_template", "name": "finance", "description": "d"}
    if version is not None:
        manifest["version"] = version
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> interface_deep.JiuWenSwarmDeepAdapter:
    package_dir = tmp_path / "finance"
    _write_manifest(package_dir, "1.0.0")
    monkeypatch.setattr(equipment, "resolve_agent_template_dir", lambda _name: package_dir)
    instance = SimpleNamespace(
        load_agent_template=AsyncMock(side_effect=lambda path: f"record:{path}"),
        unload_extension=AsyncMock(),
    )
    deep = interface_deep.JiuWenSwarmDeepAdapter()
    deep._instance = instance
    deep._loaded_agent_template = None
    deep.package_dir = package_dir
    return deep


@pytest.mark.asyncio
async def test_same_expert_twice_loads_once_and_a_new_version_reloads(adapter) -> None:
    params = {"agent_template_name": "finance"}
    await adapter._load_agent_template_for_request(params)
    await adapter._load_agent_template_for_request(params)
    assert adapter._instance.load_agent_template.await_count == 1
    assert adapter._loaded_agent_template[2] == "1.0.0"

    _write_manifest(adapter.package_dir, "1.1.0")
    await adapter._unload_agent_template_for_request(params)
    adapter._instance.unload_extension.assert_awaited_once()
    await adapter._load_agent_template_for_request(params)
    assert adapter._instance.load_agent_template.await_count == 2
    assert adapter._loaded_agent_template[2] == "1.1.0"


@pytest.mark.asyncio
async def test_unchanged_version_keeps_the_loaded_expert(adapter) -> None:
    params = {"agent_template_name": "finance"}
    await adapter._load_agent_template_for_request(params)
    await adapter._unload_agent_template_for_request(params)
    adapter._instance.unload_extension.assert_not_awaited()


@pytest.mark.asyncio
async def test_version_reads_as_empty_like_before(tmp_path: Path) -> None:
    """Missing / unreadable / version-less manifests read as "" — the old read_manifest_version result."""
    assert await interface_deep._agent_template_version(tmp_path / "absent") == ""
    _write_manifest(tmp_path / "plain", None)
    assert await interface_deep._agent_template_version(tmp_path / "plain") == ""
    _write_manifest(tmp_path / "spaced", " 2.0 ")
    assert await interface_deep._agent_template_version(tmp_path / "spaced") == equipment.read_manifest_version(
        tmp_path / "spaced"
    )
