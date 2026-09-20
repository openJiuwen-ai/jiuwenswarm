# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for PolicyReader.load_policy platform merge vs replace."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenbox.server.policy_reader import PolicyReader


_BASE_POLICY = {
    "version": 1,
    "name": "bundled-base",
    "filesystem_policy": {
        "directories": [{"path": "/home", "permissions": "0777"}],
        "read_write": ["/home"],
    },
}

_OVERRIDE_POLICY = {
    "version": 1,
    "name": "enterprise-override",
    "filesystem_policy": {
        "directories": [{"path": "/tmp", "permissions": "1777"}],
        "read_write": ["/tmp"],
    },
}


def _dir_paths(policy) -> list[str]:
    return [
        item if isinstance(item, str) else item.path
        for item in policy.filesystem_policy.directories
    ]


@pytest.fixture
def policy_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base_path = tmp_path / "default-policy.yaml"
    override_path = tmp_path / "enterprise-policy.yaml"
    base_path.write_text(yaml.safe_dump(_BASE_POLICY), encoding="utf-8")
    override_path.write_text(yaml.safe_dump(_OVERRIDE_POLICY), encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenbox.server.policy_reader.base_policy_path",
        lambda: base_path,
    )
    return base_path, override_path


def test_linux_load_policy_replaces_base(policy_files, monkeypatch: pytest.MonkeyPatch):
    _base_path, override_path = policy_files
    monkeypatch.setattr("jiuwenbox.server.policy_reader.sys.platform", "linux")

    policy = PolicyReader(policy_path=override_path).load_policy()

    assert policy.name == "enterprise-override"
    assert _dir_paths(policy) == ["/tmp"]
    assert policy.filesystem_policy.read_write == ["/tmp"]


def test_windows_load_policy_merges_base(policy_files, monkeypatch: pytest.MonkeyPatch):
    _base_path, override_path = policy_files
    monkeypatch.setattr("jiuwenbox.server.policy_reader.sys.platform", "win32")

    policy = PolicyReader(policy_path=override_path).load_policy()

    assert _dir_paths(policy) == ["/home", "/tmp"]
    assert policy.filesystem_policy.read_write == ["/home", "/tmp"]


def test_missing_override_falls_back_to_base(policy_files, monkeypatch: pytest.MonkeyPatch):
    base_path, _override_path = policy_files
    monkeypatch.setattr("jiuwenbox.server.policy_reader.sys.platform", "linux")

    policy = PolicyReader(policy_path=base_path.parent / "missing.yaml").load_policy()

    assert policy.name == "bundled-base"
    assert _dir_paths(policy) == ["/home"]
