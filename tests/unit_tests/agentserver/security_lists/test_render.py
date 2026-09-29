# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists_render 单测：名单 → 沙箱副本六列表映射（设计 4.4 数据源切换）。"""
from __future__ import annotations

import pytest
import yaml

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    SecurityListRecord,
)
from jiuwenswarm.server import sandbox_policy_render as spr
from jiuwenswarm.server import security_lists_render as render


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 config.yaml + 沙箱运行时副本落点。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: cfg_path)
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    monkeypatch.setattr(spr, "_runtime_copy_path", lambda: copy_path)
    return {"cfg": cfg_path, "copy": copy_path}


def rec(**kw) -> SecurityListRecord:
    base = dict(type="file_path", pattern="C:/data", match="prefix", cells={})
    base.update(kw)
    return SecurityListRecord(**base)


# ---------------------------------------------------------------------------
# collect_sandbox_lists（纯函数）
# ---------------------------------------------------------------------------


def test_collect_file_path_axes(env):
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny", "write": "allow"}}))
    store.upsert_record(rec(pattern="C:/locked", cells={"*": {"write": "deny"}}))
    store.upsert_record(rec(pattern="C:/asked", cells={"*": {"read": "ask"}}))  # ask 无沙箱语义
    store.upsert_record(rec(pattern="C:/exec-only", cells={"*": {"exec": "deny"}}))  # exec 不渲染
    store.upsert_record(rec(pattern="C:/off", enabled=False, cells={"*": {"read": "deny"}}))

    out = render.collect_sandbox_lists(mode="*")
    assert out["deny_read"] == ["C:/secret"]
    assert out["deny_write"] == ["C:/locked"]
    assert out["allow_read"] == []
    assert out["allow_write"] == ["C:/secret"]


def test_collect_domain_lists(env):
    store.upsert_record(rec(type="domain", pattern="*.Evil.com", match="wildcard",
                            cells={"*": {"*": "deny"}}))
    store.upsert_record(rec(type="domain", pattern="Mirror.Org", match="exact",
                            cells={"*": {"*": "allow"}}))

    out = render.collect_sandbox_lists(mode="*")
    assert out["blocked_domains"] == ["*.evil.com"]  # 域名落盘统一小写
    assert out["allowed_domains"] == ["mirror.org"]


def test_collect_mode_resolution(env):
    store.upsert_record(rec(pattern="C:/multi", cells={
        "*": {"read": "allow"},
        "full_access": {"read": "deny"},
    }))

    assert render.collect_sandbox_lists(mode="default")["allow_read"] == ["C:/multi"]
    out = render.collect_sandbox_lists(mode="full_access")
    assert out["deny_read"] == ["C:/multi"]
    assert out["allow_read"] == []


def test_collect_deny_priority_across_records(env):
    store.upsert_record(rec(pattern="C:/conflict", cells={"*": {"read": "allow"}}))
    # 同对象另一条记录（不同 match 形式）给 deny → deny 优先从 allow 剔除
    store.upsert_record(rec(pattern="C:/conflict", match="glob", cells={"*": {"read": "deny"}}))
    store.upsert_record(rec(type="domain", pattern="x.com", match="exact", cells={"*": {"*": "allow"}}))
    store.upsert_record(rec(type="domain", pattern="X.com", match="exact",
                            cells={"full_access": {"*": "deny"}}))

    out = render.collect_sandbox_lists(mode="*")
    assert out["deny_read"] == ["C:/conflict"]
    assert out["allow_read"] == []
    # 模式未命中 deny 格 → 仅 allow
    assert out["allowed_domains"] == ["x.com"]
    out2 = render.collect_sandbox_lists(mode="full_access")
    assert out2["blocked_domains"] == ["x.com"]
    assert out2["allowed_domains"] == []


# ---------------------------------------------------------------------------
# render_sandbox_copy（写副本）
# ---------------------------------------------------------------------------


def _read_copy(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_render_writes_copy_preserving_disable_all(env):
    # 预置副本：disable_all 总开关 + 陈旧用户段（应被整段替换）
    env["copy"].write_text(yaml.safe_dump({
        "windows": {
            "filesystem": {"allow_read": ["C:/stale"], "allow_write": ["C:/stale"],
                           "deny_read": [], "deny_write": []},
            "network": {"disable_all": True,
                        "egress": {"allowed_domains": ["old.com"], "blocked_domains": []}},
        }
    }), encoding="utf-8")
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny", "write": "deny"}}))
    store.upsert_record(rec(type="domain", pattern="ok.com", match="exact", cells={"*": {"*": "allow"}}))

    counts = render.render_sandbox_copy(mode="*")
    assert counts["deny_read"] == 1 and counts["allowed_domains"] == 1

    data = _read_copy(env["copy"])
    fs = data["windows"]["filesystem"]
    assert fs["deny_read"] == ["C:/secret"]
    assert fs["deny_write"] == ["C:/secret"]
    assert fs["allow_read"] == []  # 陈旧段被整段替换
    net = data["windows"]["network"]
    assert net["disable_all"] is True  # 总开关不碰
    assert net["egress"]["allowed_domains"] == ["ok.com"]


def test_render_skips_non_absolute_paths(env):
    store.upsert_record(rec(pattern="*.pem", match="glob", cells={"*": {"read": "deny"}}))
    store.upsert_record(rec(pattern="C:/abs", cells={"*": {"read": "deny"}}))

    render.render_sandbox_copy(mode="*")
    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    assert fs["deny_read"] == ["C:/abs"]  # 相对路径校验跳过，不影响合法条目
