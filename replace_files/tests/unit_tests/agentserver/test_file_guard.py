# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``file_guard`` (phase 1)."""

from jiuwenclaw.agentserver.permissions.file_guard import (
    FileGuardChecker,
    merged_file_guard_config,
    report_legacy_path_rules_at_load,
)
from jiuwenclaw.agentserver.permissions.models import PermissionLevel


def test_merged_file_guard_migrates_external_directory_allow():
    permissions = {
        "external_directory": {
            "*": "ask",
            "/data/shared": "allow",
        },
        "file_guard": {
            "global": {},
            "workspace": {"rw_enabled": True},
        },
    }
    fg = merged_file_guard_config(permissions)
    entry = fg["global"]["/data/shared"]
    assert entry["read_enable"] is True
    assert entry["write_enable"] is True


def test_merged_file_guard_does_not_mutate_input():
    """merged_file_guard_config 须视入参为只读，禁止就地写回 file_guard.global。"""
    permissions = {
        "external_directory": {"/data/shared": "allow"},
        "file_guard": {"global": {}, "workspace": {"rw_enabled": True}},
    }
    original_global = permissions["file_guard"]["global"]
    snapshot = dict(original_global)

    merged = merged_file_guard_config(permissions)

    assert "/data/shared" in merged["global"]
    assert original_global == snapshot
    assert "/data/shared" not in original_global
    assert permissions["file_guard"]["global"] is original_global


def test_merged_file_guard_migrates_external_ask():
    permissions = {
        "external_directory": {"*": "ask", "/secret": "ask"},
        "file_guard": {"global": {}},
    }
    fg = merged_file_guard_config(permissions)
    assert fg["global"]["/secret"]["read_enable"] is False


def test_file_guard_inside_workspace_noop(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    f = ws / "a.txt"
    f.write_text("x", encoding="utf-8")
    checker = FileGuardChecker(
        {"file_guard": {"workspace": {"rw_enabled": True}, "global": {}}},
        workspace_root=ws,
    )
    assert checker.check_external_paths("read_file", {"file_path": str(f)}) is None


def test_file_guard_outside_workspace_asks(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.txt"
    f.write_text("y", encoding="utf-8")
    checker = FileGuardChecker(
        {"file_guard": {"workspace": {"rw_enabled": True}, "global": {}}},
        workspace_root=ws,
    )
    res = checker.check_external_paths("read_file", {"file_path": str(f)})
    assert res is not None
    assert res.permission == PermissionLevel.ASK
    assert "file_guard" in (res.matched_rule or "")
    assert res.file_operations


def test_file_guard_global_read_allow(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "c.txt"
    f.write_text("z", encoding="utf-8")
    prefix = str(outside.resolve())
    checker = FileGuardChecker(
        {
            "file_guard": {
                "workspace": {"rw_enabled": True},
                "global": {prefix: {"read_enable": True, "write_enable": False}},
            },
        },
        workspace_root=ws,
    )
    assert checker.check_external_paths("read_file", {"file_path": str(f)}) is None


def test_report_legacy_path_rules_flags_path_class_rules(caplog):
    """Phase-1：``rules[*]`` 残留 ``read_file + **/.ssh/**`` 这类 path 类条目，应被 ERROR 标出。"""
    permissions = {
        "rules": [
            {
                "id": "legacy_block_ssh",
                "tools": ["read_file", "Read"],
                "pattern": "**/.ssh/**",
                "action": "deny",
            },
            {
                "id": "shell_rule_keep",
                "tools": ["mcp_exec_command"],
                "pattern": "rm -rf *",
                "action": "deny",
            },
        ],
    }

    with caplog.at_level("ERROR", logger="jiuwenclaw.agentserver.permissions.file_guard"):
        flagged = report_legacy_path_rules_at_load(permissions)

    assert "legacy_block_ssh" in flagged
    assert "shell_rule_keep" not in flagged


def test_file_guard_trusted_exec(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "x.py"
    script.write_text("print(1)\n", encoding="utf-8")
    checker = FileGuardChecker(
        {
            "file_guard": {
                "workspace": {"rw_enabled": True},
                "global": {},
                "trusted_exec_directory": [str(scripts.resolve())],
            },
        },
        workspace_root=ws,
    )
    assert checker.check_external_paths(
        "mcp_exec_command",
        {"command": f'python "{script}"', "workdir": str(ws)},
    ) is None
