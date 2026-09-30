# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.normalize 单测：builtin 投影、审批投影（mode 映射）、沙箱副本投影。"""
from __future__ import annotations

import yaml

from jiuwenswarm.agents.harness.common.rails.security_lists.normalize import (
    project_approvals,
    project_builtin,
    project_net_guard,
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


def test_project_approvals_file_guard_disabled_skips_paths():
    """file_guard.enabled=false（面板「启用文件安全护栏」关闭）→ 路径规则不投影。

    引擎侧 ``build_file_guard_checker`` 在 enabled 为假时返回 None（路径层整层
    不生效），名单侧若仍按 paths 判定，则用户关掉开关后引擎放行、rail 继续拦。
    """
    perms = {
        "approval_overrides": [
            {"id": "ov1", "match_type": "command", "pattern": "git status *",
             "action": "allow"},
        ],
        "file_guard": {
            "enabled": False,
            "paths": [{"path": "C:/data", "read": "allow", "write": "deny",
                       "match": "prefix"}],
        },
    }
    records = project_approvals(permissions=perms)
    assert [r.id for r in records] == ["ov1"]


def test_project_approvals_file_guard_enabled_true_keeps_paths():
    """显式 enabled=true 与键缺省都照旧投影（模板默认 true）。"""
    for file_guard in (
        {"enabled": True, "paths": [{"path": "C:/on", "read": "allow"}]},
        {"paths": [{"path": "C:/absent", "read": "allow"}]},
    ):
        records = project_approvals(permissions={"file_guard": file_guard})
        assert len(records) == 1, file_guard


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


# ---------------------------------------------------------------------------
# net_guard 兼容投影（S1：域名双向可见）
# ---------------------------------------------------------------------------


def test_project_net_guard_urls_as_user_domain_records():
    perms = {
        "net_guard": {
            "enabled": True,
            "urls": {
                "evil.example": "deny",
                "*.ok.example": "allow",
                "widened.example": "block",   # 非法动作 → 跳过
            },
        },
    }
    records = {r.pattern: r for r in project_net_guard(permissions=perms)}
    assert set(records) == {"evil.example", "*.ok.example"}
    evil = records["evil.example"]
    assert evil.type == "domain" and evil.match == "exact"
    assert evil.cells == {"*": {"*": "deny"}}       # net_guard 无模式档 → 通用格
    assert evil.source == "user"                     # 用户可删可改
    assert records["*.ok.example"].match == "wildcard"


def test_project_net_guard_disabled_skips_all():
    """net_guard.enabled=false（面板总开关关）→ 不投影，与 file_guard.enabled 同判据。"""
    perms = {"net_guard": {"enabled": False, "urls": {"evil.example": "deny"}}}
    assert project_net_guard(permissions=perms) == []


def test_project_net_guard_absent_or_malformed_is_empty():
    assert project_net_guard(permissions={}) == []
    assert project_net_guard(permissions={"net_guard": "oops"}) == []
    assert project_net_guard(permissions={"net_guard": {"urls": ["x"]}}) == []
    assert project_net_guard(permissions={"net_guard": {"urls": {"": "deny"}}}) == []


# ---------------------------------------------------------------------------
# occupied 让位（S3 写面收敛：物理记录接管后 legacy 投影不再生效）
# ---------------------------------------------------------------------------


def test_project_net_guard_skips_keys_occupied_by_physical_records():
    """已被 security_lists.user 接管的操作对象，legacy 投影让位（否则新面板删不掉）。"""
    perms = {"net_guard": {"enabled": True, "urls": {
        "evil.example": "deny", "ok.example": "allow",
    }}}
    occupied = {("domain", "evil.example", "exact")}

    records = project_net_guard(permissions=perms, occupied=occupied)

    assert {r.pattern for r in records} == {"ok.example"}


def test_project_approvals_skips_occupied_file_paths_but_keeps_overrides():
    """让位只作用于被接管的操作对象；审批条目（另一类通道）不受影响。"""
    perms = {
        "approval_overrides": [
            {"id": "ov1", "match_type": "command", "pattern": "git *", "action": "allow"},
        ],
        "file_guard": {"paths": [{"path": "C:/data", "read": "allow"}]},
    }
    occupied = {("file_path", "C:/data", "prefix")}

    records = project_approvals(permissions=perms, occupied=occupied)

    assert [r.id for r in records] == ["ov1"]


def test_project_approvals_occupied_defaults_to_nothing_skipped():
    """缺省 occupied（banner/RPC 卡片视图等只读消费方）→ 全量投影，行为不变。"""
    perms = {"file_guard": {"paths": [{"path": "C:/data", "read": "allow"}]}}
    assert len(project_approvals(permissions=perms)) == 1
