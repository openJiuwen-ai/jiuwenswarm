# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SecurityListComposer 收集范围：清单来源与"不进运行时判定"的项。"""
from __future__ import annotations

import yaml

from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.composer import (
    SecurityListComposer,
)


def test_collect_excludes_sandbox_runtime_copy(tmp_path, monkeypatch):
    """沙箱运行时副本（windows-policy.runtime.yaml）不进运行时收集。

    该副本是 sandbox.files.set / sandbox.network.set 与 FileGuard 同步的**活配置**
    （server/sandbox_policy_render.py 直接读写它）；迁移（store.migrate_sandbox_copy_once）
    已把它搬进 security_lists.user，运行时再投影一次会与之双重判定、并让
    "迁移清空副本"反过来抹掉沙箱配置。
    """
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    copy_path.write_text(
        yaml.safe_dump(
            {
                "windows": {
                    "filesystem": {"allow_read": ["C:/probe-only-in-copy"]},
                    "network": {
                        "egress": {"blocked_domains": ["*.probe-only-in-copy.com"]}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "_default_copy_path", lambda: copy_path)

    records = SecurityListComposer().collect()

    assert not [r for r in records if r.migrated_from == "sandbox_copy"]
    assert not [r for r in records if r.pattern == "C:/probe-only-in-copy"]
    assert not [r for r in records if r.pattern == "*.probe-only-in-copy.com"]


def test_collect_includes_net_guard_urls(monkeypatch):
    """S1 域名读归一：存量 ``permissions.net_guard.urls`` 进统一视图（否则迁移后规则静默失效）。"""
    import jiuwenswarm.common.config as config_mod
    from jiuwenswarm.agents.harness.common.rails.security_lists import (
        composer as composer_mod,
    )
    from jiuwenswarm.agents.harness.common.rails.security_lists.normalize import (
        project_builtin as real_builtin,
    )

    monkeypatch.setattr(composer_mod, "project_builtin", real_builtin)
    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: {
            "permissions": {
                "net_guard": {"enabled": True, "urls": {"evil.example": "deny"}}
            }
        },
    )

    records = SecurityListComposer().collect("domain")

    hit = [r for r in records if r.pattern == "evil.example"]
    assert len(hit) == 1
    assert hit[0].cells == {"*": {"*": "deny"}} and hit[0].source == "user"


def test_collect_user_record_shadows_projected_legacy(tmp_path, monkeypatch):
    """S3 写面收敛：物理 user 记录接管同操作对象后，legacy 投影让位。

    否则迁移后 legacy 段仍投影出第二条同名记录：新面板删掉物理记录也拦不住，
    用户看到"删了还在拦"，且弹窗/审计会出现同一规则的两条 hit。
    """
    import jiuwenswarm.common.config as config_mod

    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "security_lists": {
                "version": 3,
                "user": [{
                    "id": "ul_takeover", "type": "domain", "pattern": "evil.example",
                    "match": "exact", "enabled": True, "cells": {"*": {"*": "deny"}},
                }],
            },
            "permissions": {
                "net_guard": {"enabled": True, "urls": {"evil.example": "allow"}},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: path)

    hit = [
        r for r in SecurityListComposer().collect("domain")
        if r.type == "domain" and r.pattern == "evil.example"
    ]

    assert len(hit) == 1
    assert hit[0].id == "ul_takeover"          # 物理记录为准
    assert hit[0].cells == {"*": {"*": "deny"}}  # legacy 的 allow 不再参与
