# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""permissions_persist 写入 ``file_guard.paths``（对齐 agent-core §5.5.6）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_permissions_persist():
    """直接加载模块，避免 rails/__init__ 拉起 team 依赖。"""
    root = Path(__file__).resolve().parents[4]  # jiuwenswarm package root's parent
    # tests/unit_tests/agents/harness -> 4 up = repo root? 
    # __file__ = .../tests/unit_tests/agents/harness/test_....py
    # parents[0]=harness, [1]=agents, [2]=unit_tests, [3]=tests, [4]=repo
    mod_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "permissions"
        / "permissions_persist.py"
    )
    spec = importlib.util.spec_from_file_location(
        "permissions_persist_under_test", mod_path
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def config_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "permissions:\n  enabled: true\n  external_directory:\n    '*': ask\n",
        encoding="utf-8",
    )
    from jiuwenswarm.common import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg)
    if hasattr(cfg_mod, "CONFIG_YAML_PATH"):
        monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg)
    return cfg


def test_persist_cli_trusted_directory_writes_file_guard(config_yaml, tmp_path):
    pp = _load_permissions_persist()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    result = pp.persist_cli_trusted_directory(str(trusted))
    assert result.get("ok") is True

    from jiuwenswarm.common.config import _load_yaml_round_trip

    data = _load_yaml_round_trip(config_yaml)
    perms = data["permissions"]
    fg = perms["file_guard"]
    dir_norm = trusted.resolve().as_posix().rstrip("/")
    assert any(
        isinstance(p, dict)
        and str(p.get("path", "")).replace("\\", "/").rstrip("/") == dir_norm
        and p.get("read") == "allow"
        and p.get("write") == "allow"
        and p.get("exec") == "allow"
        for p in fg["paths"]
    )
    overlay = config_yaml.with_name("config.user.yaml")
    assert not overlay.is_file()


def test_persist_cli_trusted_directory_with_overrides_keeps_shell_only(config_yaml, tmp_path):
    pp = _load_permissions_persist()
    from jiuwenswarm.common.config import _load_yaml_round_trip

    trusted = tmp_path / "trusted2"
    trusted.mkdir()
    result = pp.persist_cli_trusted_directory_with_overrides(str(trusted))
    assert result.get("ok") is True

    data = _load_yaml_round_trip(config_yaml)
    perms = data["permissions"]
    overrides = perms.get("approval_overrides") or []
    assert not any(isinstance(o, dict) and o.get("match_type") == "path" for o in overrides)
    assert any(isinstance(o, dict) and o.get("match_type") == "command" for o in overrides)


def test_persist_external_directory_allow_writes_file_guard(config_yaml, tmp_path):
    pp = _load_permissions_persist()
    from jiuwenswarm.common.config import _load_yaml_round_trip

    dir_path = tmp_path / "outside" / "projects"
    dir_path.mkdir(parents=True)
    pp.persist_external_directory_allow([str(dir_path)])
    data = _load_yaml_round_trip(config_yaml)
    fg = data["permissions"]["file_guard"]
    assert isinstance(fg.get("paths"), list) and fg["paths"]
    dir_norm = str(dir_path).replace("\\", "/").rstrip("/")
    parent_norm = str(dir_path.parent).replace("\\", "/").rstrip("/")
    assert any(
        isinstance(p, dict)
        and str(p.get("path", "")).replace("\\", "/").rstrip("/") == dir_norm
        and p.get("read") == "allow"
        and p.get("write") == "ask"
        and p.get("exec") == "ask"
        for p in fg["paths"]
    )
    assert not any(
        isinstance(p, dict)
        and str(p.get("path", "")).replace("\\", "/").rstrip("/") == parent_norm
        for p in fg["paths"]
    )


def test_persist_merged_allow_rule_snapshot_writes_yaml_not_overlay(
    config_yaml, tmp_path
):
    pp = _load_permissions_persist()
    from jiuwenswarm.common.config import _load_yaml_round_trip

    ok = pp.persist_merged_allow_rule_snapshot(
        {
            "approval_overrides": [
                {
                    "id": "curl_post",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "curl *",
                    "action": "allow",
                }
            ],
            "file_guard": {"paths": [{"path": "C:/docs", "write": "allow"}]},
        }
    )
    assert ok is True
    system = _load_yaml_round_trip(config_yaml)
    assert system["permissions"]["approval_overrides"][0]["id"] == "curl_post"
    assert system["permissions"]["file_guard"]["paths"][0]["path"] == "C:/docs"
    overlay = config_yaml.with_name("config.user.yaml")
    assert not overlay.is_file()
