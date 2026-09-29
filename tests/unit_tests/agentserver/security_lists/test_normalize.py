# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.normalize 单测：builtin 投影、审批投影（mode 映射）、沙箱副本投影。"""
from __future__ import annotations

import yaml

from jiuwenswarm.agents.harness.common.rails.security_lists.normalize import (
    project_approvals,
    project_builtin,
    project_sandbox_runtime_copy,
)


# ---------------------------------------------------------------------------
# builtin
# ---------------------------------------------------------------------------


def test_project_builtin_loads_package_rules():
    records = {r.id: r for r in project_builtin()}
    # 命令规则：re: 前缀剥离 → regex
    reg = records["shell_registry_delete"]
    assert reg.type == "command" and reg.match == "regex"
    assert reg.pattern.startswith("(?i)") and not reg.pattern.startswith("re:")
    assert reg.cells == {"*": {"*": "deny"}} and reg.source == "builtin"
    # 敏感路径：三轴写入
    ssh = records["home_ssh"]
    assert ssh.type == "file_path" and ssh.match == "glob"
    assert ssh.cells == {"*": {"read": "deny", "write": "deny", "exec": "deny"}}
    # 网络内置
    localhost = records["builtin_net_localhost"]
    assert localhost.type == "domain" and localhost.match == "exact"
    assert localhost.cells == {"*": {"*": "deny"}}


# ---------------------------------------------------------------------------
# 审批投影
# ---------------------------------------------------------------------------


def test_project_approvals_command_override_without_mode_goes_general_cell():
    perms = {"approval_overrides": [
        {"id": "ov1", "tools": ["bash"], "match_type": "command",
         "pattern": "git status *", "action": "allow"},
    ]}
    records = project_approvals(permissions=perms)
    assert len(records) == 1
    rec = records[0]
    assert rec.id == "ov1" and rec.source == "user_approval"
    assert rec.type == "command" and rec.match == "glob"
    assert rec.cells == {"*": {"*": "allow"}}


def test_project_approvals_command_override_with_mode_goes_mode_cell():
    perms = {"approval_overrides": [
        {"id": "ov2", "match_type": "command", "pattern": "npm *",
         "action": "allow", "mode": "auto_approve", "created_at": "2026-09-28T00:00:00+00:00"},
    ]}
    rec = project_approvals(permissions=perms)[0]
    assert rec.cells == {"auto_approve": {"*": "allow"}}
    assert rec.created_at == "2026-09-28T00:00:00+00:00"


def test_project_approvals_skips_path_and_invalid_overrides():
    perms = {"approval_overrides": [
        {"id": "p1", "match_type": "path", "pattern": "C:/x", "action": "allow"},   # path 类跳过
        {"id": "p2", "match_type": "command", "pattern": "git *", "action": "block"},  # 非法动作
        {"id": "p3", "match_type": "command", "pattern": "", "action": "allow"},    # 空 pattern
        "garbage",
    ]}
    assert project_approvals(permissions=perms) == []


def test_project_approvals_file_guard_axes_to_cells():
    perms = {"file_guard": {"paths": [
        {"path": "C:/data", "read": "allow", "write": "deny", "exec": "ask",
         "match": "prefix", "mode": "default"},
    ]}}
    rec = project_approvals(permissions=perms)[0]
    assert rec.type == "file_path" and rec.pattern == "C:/data" and rec.match == "prefix"
    assert rec.cells == {"default": {"read": "allow", "write": "deny", "exec": "ask"}}
    assert rec.source == "user_approval"
    assert rec.id.startswith("fg_")  # 无 id 时按路径合成稳定 id


def test_project_approvals_file_guard_partial_axes_and_defaults():
    perms = {"file_guard": {"paths": [
        {"path": "C:/partial", "read": "allow"},                     # 缺省轴不出现
        {"path": "C:/badmatch", "read": "allow", "match": "weird"},  # 非法 match → prefix
        {"path": "C:/empty"},                                        # 无有效轴 → 跳过
    ]}}
    records = {r.pattern: r for r in project_approvals(permissions=perms)}
    assert set(records) == {"C:/partial", "C:/badmatch"}
    assert records["C:/partial"].cells == {"*": {"read": "allow"}}
    assert records["C:/badmatch"].match == "prefix"


# ---------------------------------------------------------------------------
# 沙箱副本投影
# ---------------------------------------------------------------------------


def test_project_sandbox_runtime_copy(tmp_path):
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    copy_path.write_text(yaml.safe_dump({
        "windows": {
            "filesystem": {"allow_read": ["C:/data"], "deny_write": ["C:/data"]},
            "network": {"egress": {"blocked_domains": ["*.evil.com"]}},
        },
    }), encoding="utf-8")
    records = {r.pattern: r for r in project_sandbox_runtime_copy(copy_path)}
    assert records["C:/data"].cells == {"*": {"read": "allow", "write": "deny"}}
    assert records["C:/data"].source == "user"
    assert records["*.evil.com"].cells == {"*": {"*": "deny"}}


def test_project_sandbox_runtime_copy_missing_or_empty(tmp_path):
    assert project_sandbox_runtime_copy(tmp_path / "nope.yaml") == []
    empty = tmp_path / "empty.yaml"
    empty.write_text("windows: {}\n", encoding="utf-8")
    assert project_sandbox_runtime_copy(empty) == []
