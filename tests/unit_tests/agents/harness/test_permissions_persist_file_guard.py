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

    monkeypatch.setattr(cfg_mod, "get_config_file", lambda: cfg)
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg)
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
    assert fg["enabled"] is True
    dir_norm = trusted.resolve().as_posix().rstrip("/")
    assert any(
        isinstance(p, dict)
        and str(p.get("path", "")).replace("\\", "/").rstrip("/") == dir_norm
        and p.get("read") == "allow"
        and p.get("write") == "allow"
        and p.get("exec") == "ask"
        for p in fg["paths"]
    )
    ext = perms.get("external_directory") or {}
    assert ext.get(dir_norm) != "allow"
    assert ext.get(str(trusted.resolve())) != "allow"


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


def test_derive_user_overlay_delta_ratchets_against_disk() -> None:
    # round-024：上游 rail 传整段合并 cfg，host 侧须 diff 出真增量——
    # 只棘轮放大项入 user 层，全局 preset 同值项不得整段复制。
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _derive_user_overlay_delta,
    )

    disk_global = {
        "tools": {"bash": "ask"},
        "file_guard": {
            "paths": [
                {
                    "path": "**/.ssh/**",
                    "match": "glob",
                    "read": "deny",
                    "write": "deny",
                    "exec": "deny",
                },
                {"path": "C:/secret", "match": "prefix", "read": "ask", "write": "ask", "exec": "deny"},
            ]
        },
    }
    template = {
        "file_guard": {
            "paths": [
                {
                    "path": "**/.ssh/**",
                    "match": "glob",
                    "read": "deny",
                    "write": "deny",
                    "exec": "deny",
                }
            ]
        }
    }
    user = {"allow_tools": ["todo_list"], "ask_tools": ["memory_get"]}
    cfg = {
        "tools": {
            "list_files": "allow",  # 磁盘未配置 → allow 增量
            "bash": "ask",  # 与磁盘生效同值 → 排除
            "todo_list": "allow",  # user allow_tools 已含 → 排除
        },
        "file_guard": {
            "paths": [
                {  # preset 同值 → 排除
                    "path": "**/.ssh/**",
                    "match": "glob",
                    "read": "deny",
                    "write": "deny",
                    "exec": "deny",
                },
                {  # read 轴放大 ask→allow → 入增量
                    "path": "C:/secret",
                    "read": "allow",
                    "write": "ask",
                    "exec": "deny",
                },
                {"path": "C:/newdir", "read": "allow", "write": "allow", "exec": "ask"},
                {"path": "C:\\back\\dir", "read": "allow"},  # 反斜杠归一化 + 新路径
            ]
        },
    }

    payload = _derive_user_overlay_delta(cfg, [disk_global, template, user])
    assert payload["_allow_tools_added"] == ["list_files"]
    added = {e["path"].replace("\\", "/") for e in payload["_file_guard_paths_added"]}
    assert added == {"C:/secret", "C:/newdir", "C:/back/dir"}
    assert "**/.ssh/**" not in added

    # deny 放大通道：并入 user 现值（persist 侧整表替换）
    payload_deny = _derive_user_overlay_delta(
        {"tools": {"legacy_tool": "deny"}},
        [disk_global, template, user],
    )
    assert payload_deny["deny_tools"] == ["legacy_tool"]
    assert "_file_guard_paths_added" not in payload_deny
