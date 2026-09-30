# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists_render 单测：名单 → 沙箱副本六列表映射（设计 4.4 数据源切换）。"""
from __future__ import annotations

import json

import pytest
import yaml

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import audit, store
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    SecurityListRecord,
)
from jiuwenswarm.server import sandbox_policy_render as spr
from jiuwenswarm.server import security_lists_render as render


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 config.yaml + 沙箱运行时副本落点 + 审计文件。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: cfg_path)
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    monkeypatch.setattr(spr, "_runtime_copy_path", lambda: copy_path)
    audit_path = tmp_path / "security_audit.jsonl"
    monkeypatch.setattr(audit, "_audit_file", lambda: audit_path)
    return {"cfg": cfg_path, "copy": copy_path, "audit": audit_path}


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
    # 副本是空骨架（没有会被丢掉的条目）→ 正常整段替换
    _seed_copy(env["copy"], disable_all=True)
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny", "write": "deny"}}))
    store.upsert_record(rec(type="domain", pattern="ok.com", match="exact", cells={"*": {"*": "allow"}}))

    counts = render.render_sandbox_copy(mode="*")
    assert counts["deny_read"] == 1 and counts["allowed_domains"] == 1

    data = _read_copy(env["copy"])
    fs = data["windows"]["filesystem"]
    assert fs["deny_read"] == ["C:/secret"]
    assert fs["deny_write"] == ["C:/secret"]
    assert fs["allow_read"] == []
    net = data["windows"]["network"]
    assert net["disable_all"] is True  # 总开关不碰
    assert net["egress"]["allowed_domains"] == ["ok.com"]


# ---------------------------------------------------------------------------
# 渲染前安全检查：会丢掉副本里已有条目时，宁可不渲染（模块 docstring「为何要跳过」）
# ---------------------------------------------------------------------------


def _seed_copy(path, *, allow_read=(), deny_write=(), blocked_domains=(), disable_all=False):
    path.write_text(yaml.safe_dump({
        "windows": {
            "filesystem": {"allow_read": list(allow_read), "allow_write": [],
                           "deny_read": [], "deny_write": list(deny_write)},
            "network": {"disable_all": disable_all,
                        "egress": {"allowed_domains": [],
                                   "blocked_domains": list(blocked_domains)}},
        }
    }, allow_unicode=True), encoding="utf-8")


def test_render_skips_when_it_would_drop_unmanaged_copy_entries(env):
    """副本里有名单不知道的条目（沙箱面板 / /add-dir / FileGuard sync 写的）→ 不渲染。

    否则第一次名单写入就会把这些 ACL/egress 静默清空，还会触发 box-server 重载
    ——用户沙箱策略真的没了。这条是实测复现过的数据丢失路径。
    """
    _seed_copy(env["copy"], allow_read=["C:/users-data"],
               blocked_domains=["realmalware.example"])
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny"}}))

    counts = render.render_sandbox_copy(mode="*")

    data = _read_copy(env["copy"])
    fs = data["windows"]["filesystem"]
    net = data["windows"]["network"]
    assert fs["allow_read"] == ["C:/users-data"]        # 副本原样
    assert fs["deny_read"] == []                         # 名单里的规则**没有**下发
    assert net["egress"]["blocked_domains"] == ["realmalware.example"]
    assert counts["skipped"] is True
    events = [
        json.loads(ln)
        for ln in env["audit"].read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert [e["kind"] for e in events] == [audit.AUDIT_RENDER_SKIPPED]


def test_render_does_not_skip_when_copy_entries_are_covered(env):
    """副本已有条目全在名单里 → 不触发跳过（"会丢条目"是唯一的跳过条件）。"""
    _seed_copy(env["copy"], allow_read=["C:/users-data"])
    store.upsert_record(rec(pattern="C:/users-data", cells={"*": {"read": "allow"}}))
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny"}}))

    counts = render.render_sandbox_copy(mode="*")

    assert "skipped" not in counts
    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    assert fs["allow_read"] == ["C:/users-data"]
    assert fs["deny_read"] == ["C:/secret"]


def test_render_skips_when_list_deletion_would_wipe_copy_entry(env):
    """**已知取舍（记录在案）**：名单里删掉一条、副本还留着 → 仍属"会丢条目"，跳过。

    即"从名单删除"暂时下发不到沙箱（护栏 rail 不受影响，它直读名单）。这是止血的
    代价；正解是写面收敛（沙箱面板改写 security_lists，届时副本不会再有未纳管条目）。
    """
    _seed_copy(env["copy"], allow_read=["C:/gone"])
    store.upsert_record(rec(pattern="C:/gone", cells={"*": {"read": "allow"}}))
    render.render_sandbox_copy(mode="*")
    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == ["C:/gone"]

    store.delete_record(store.get_security_lists()["user"][0].id)

    counts = render.render_sandbox_copy(mode="*")
    assert counts["skipped"] is True
    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == ["C:/gone"]


def test_render_skips_non_absolute_paths(env):
    store.upsert_record(rec(pattern="*.pem", match="glob", cells={"*": {"read": "deny"}}))
    store.upsert_record(rec(pattern="C:/abs", cells={"*": {"read": "deny"}}))

    render.render_sandbox_copy(mode="*")
    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    assert fs["deny_read"] == ["C:/abs"]  # 相对路径校验跳过，不影响合法条目
