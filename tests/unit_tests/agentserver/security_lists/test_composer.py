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
