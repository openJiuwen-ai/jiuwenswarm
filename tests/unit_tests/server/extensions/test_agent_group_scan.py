# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for pure AgentGroup directory scan."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.agents.harness.team.expert_org.agent_group_scan import scan_agent_group_dirs


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _minimal_manifest_group(root: Path, name: str) -> Path:
    package = root / name
    _write_json(
        package / "manifest.json",
        {"name": name, "package_type": "agent_group", "agents": ["leader"]},
    )
    return package


def test_scan_skips_multi_source_conflicts(tmp_path: Path) -> None:
    local = tmp_path / "local"
    resources = tmp_path / "resources"
    local.mkdir()
    resources.mkdir()
    _minimal_manifest_group(local, "dup-group")
    _minimal_manifest_group(resources, "dup-group")
    _minimal_manifest_group(local, "only-local")

    names = [
        name
        for name, _ in scan_agent_group_dirs(
            [("local", local), ("resources", resources)]
        )
    ]
    assert names == ["only-local"]


def test_scan_returns_sorted_unique_packages(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    _minimal_manifest_group(local, "beta")
    _minimal_manifest_group(local, "alpha")
    names = [name for name, _ in scan_agent_group_dirs([("local", local)])]
    assert names == ["alpha", "beta"]


def test_scan_skips_wrong_package_type(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    bad = local / "not-group"
    _write_json(bad / "manifest.json", {"name": "not-group", "package_type": "plugin"})
    _minimal_manifest_group(local, "good-group")
    names = [name for name, _ in scan_agent_group_dirs([("local", local)])]
    assert names == ["good-group"]
