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
    linux_copy = tmp_path / "default-policy.runtime.yaml"
    monkeypatch.setattr(spr, "_linux_runtime_copy_path", lambda: linux_copy)
    audit_path = tmp_path / "security_audit.jsonl"
    monkeypatch.setattr(audit, "_audit_file", lambda: audit_path)
    # 默认按 Windows 主机跑；要验 Linux 分支的用例自己把它翻过来
    monkeypatch.setattr(spr, "_is_windows", lambda: True)
    return {"cfg": cfg_path, "copy": copy_path, "linux_copy": linux_copy,
            "audit": audit_path}


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
    assert out["allowed_domains"] == ["mirror.org", "*.mirror.org"]


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
    assert out["allowed_domains"] == ["x.com", "*.x.com"]
    out2 = render.collect_sandbox_lists(mode="full_access")
    assert out2["blocked_domains"] == ["x.com", "*.x.com"]
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
    assert counts["deny_read"] == 1 and counts["allowed_domains"] == 2

    data = _read_copy(env["copy"])
    fs = data["windows"]["filesystem"]
    assert fs["deny_read"] == ["C:/secret"]
    assert fs["deny_write"] == ["C:/secret"]
    assert fs["allow_read"] == []
    net = data["windows"]["network"]
    assert net["disable_all"] is True  # 总开关不碰
    assert net["egress"]["allowed_domains"] == ["ok.com", "*.ok.com"]


# ---------------------------------------------------------------------------
# 副本单一写入者不变式：出现"未纳管条目"要留痕，但**不拦截**（见模块 docstring）
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


def test_render_warns_but_still_renders_when_copy_has_unmanaged_entries(env):
    """副本里有名单不知道的条目 → **照常渲染** + 告警 + 审计留痕（不再拦截）。

    拦截版会与写面收敛死锁：面板删一条时副本里还留着它，拦截就永远不让删除生效。
    收敛之后副本只有本模块一个写入者，这条路径正常不该发生——真出现就说明
    又有别的写入者，靠这条 WARNING/审计去排查。
    """
    _seed_copy(env["copy"], allow_read=["C:/users-data"],
               blocked_domains=["realmalware.example"])
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny"}}))

    render.render_sandbox_copy(mode="*")

    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    net = _read_copy(env["copy"])["windows"]["network"]
    assert fs["allow_read"] == []                     # 未纳管条目被覆盖（有留痕）
    assert fs["deny_read"] == ["C:/secret"]
    assert net["egress"]["blocked_domains"] == []
    events = [
        json.loads(ln)
        for ln in env["audit"].read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert [e["kind"] for e in events] == [audit.AUDIT_RENDER_DROPPED]
    assert events[0]["dropped"] == {
        "allow_read": ["C:/users-data"], "blocked_domains": ["realmalware.example"],
    }


def test_render_leaves_no_trace_when_nothing_dropped(env):
    """副本已有条目全在名单里 → 不产生 dropped 审计（别把正常渲染也记成异常）。"""
    _seed_copy(env["copy"], allow_read=["C:/users-data"])
    store.upsert_record(rec(pattern="C:/users-data", cells={"*": {"read": "allow"}}))
    store.upsert_record(rec(pattern="C:/secret", cells={"*": {"read": "deny"}}))

    render.render_sandbox_copy(mode="*")

    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    assert fs["allow_read"] == ["C:/users-data"]
    assert fs["deny_read"] == ["C:/secret"]
    assert not env["audit"].exists()


def test_render_propagates_list_deletion(env):
    """名单里删掉一条 → 副本里也删掉（写面收敛后必须能下发，见模块 docstring）。"""
    _seed_copy(env["copy"], allow_read=["C:/gone"])
    store.upsert_record(rec(pattern="C:/gone", cells={"*": {"read": "allow"}}))
    render.render_sandbox_copy(mode="*")
    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == ["C:/gone"]

    store.delete_record(store.get_security_lists()["user"][0].id)
    render.render_sandbox_copy(mode="*")

    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == []


# ---------------------------------------------------------------------------
# 写面收敛：沙箱面板的 set 改写 security_lists（副本由渲染产生）
# ---------------------------------------------------------------------------


def test_panel_files_set_writes_lists_and_renders_copy(env):
    """`sandbox.files.set` 不再直接写副本，而是写名单 → 渲染。

    面板只有 allow/deny 两列表，对应到 per-axis 模型就是 read+write 两轴同值。
    """
    result = spr.set_sandbox_files_config(["C:/data"], ["C:/secret"])

    assert result == {"allow": ["C:/data"], "deny": ["C:/secret"]}
    records = {(r.pattern, r.type, r.migrated_from): r for r in store.get_security_lists()["user"]}
    allow_rec = records[("C:/data", "file_path", "sandbox_panel")]
    deny_rec = records[("C:/secret", "file_path", "sandbox_panel")]
    assert allow_rec.cells == {"*": {"read": "allow", "write": "allow"}}
    assert deny_rec.cells == {"*": {"read": "deny", "write": "deny"}}
    # 副本由渲染产出
    fs = _read_copy(env["copy"])["windows"]["filesystem"]
    assert fs["allow_read"] == ["C:/data"] and fs["allow_write"] == ["C:/data"]
    assert fs["deny_read"] == ["C:/secret"] and fs["deny_write"] == ["C:/secret"]


def test_panel_files_set_replaces_only_its_own_records(env):
    """面板保存不能清掉安全中心配的规则（两者写的都是 user 区）。"""
    store.upsert_record(rec(pattern="C:/from-center", cells={"*": {"read": "deny"}}))
    spr.set_sandbox_files_config(["C:/panel-a"], [])
    spr.set_sandbox_files_config(["C:/panel-b"], [])

    patterns = {r.pattern for r in store.get_security_lists()["user"]}
    assert patterns == {"C:/from-center", "C:/panel-b"}   # center 的还在，panel-a 被替换掉


def test_panel_files_set_removal_propagates_to_copy(env):
    """从面板移除一条 → 名单里删掉 → 副本也删掉（这正是护栏降级换来的能力）。"""
    spr.set_sandbox_files_config(["C:/a", "C:/b"], [])
    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == ["C:/a", "C:/b"]

    spr.set_sandbox_files_config(["C:/a"], [])

    assert _read_copy(env["copy"])["windows"]["filesystem"]["allow_read"] == ["C:/a"]
    assert {r.pattern for r in store.get_security_lists()["user"]} == {"C:/a"}


def test_panel_network_set_writes_domain_records(env, monkeypatch):
    monkeypatch.setattr(spr, "_is_windows", lambda: True)

    result = spr.set_sandbox_network_config(False, ["ok.example"], ["*.evil.example"])

    assert result["allow_domains"] == ["ok.example"]
    records = {r.pattern: r for r in store.get_security_lists()["user"] if r.type == "domain"}
    assert records["ok.example"].match == "exact"          # 裸域 + 子域（与 EgressFilter 一致）
    assert records["*.evil.example"].match == "wildcard"   # 仅子域
    assert records["ok.example"].migrated_from == "sandbox_panel"
    egress = _read_copy(env["copy"])["windows"]["network"]["egress"]
    assert egress["allowed_domains"] == ["ok.example", "*.ok.example"]
    assert egress["blocked_domains"] == ["*.evil.example"]


def test_panel_network_set_keeps_disable_all_out_of_lists(env, monkeypatch):
    """disable_all 是沙箱总开关、不是名单语义，仍直接落副本（渲染不碰它）。"""
    monkeypatch.setattr(spr, "_is_windows", lambda: True)

    spr.set_sandbox_network_config(True, [], [])

    assert _read_copy(env["copy"])["windows"]["network"]["disable_all"] is True
    assert store.get_security_lists()["user"] == []


# ---------------------------------------------------------------------------
# Linux 副本（default-policy.runtime.yaml）：只有 network.egress 两列表
# ---------------------------------------------------------------------------


def test_render_linux_copy_writes_egress_from_lists(env):
    """Linux 沙箱的 egress 也必须由名单驱动（此前只有面板能写它 → 名单的域名规则到不了）。"""
    store.upsert_record(rec(type="domain", pattern="blocked.example", match="exact",
                            cells={"*": {"*": "deny"}}))
    store.upsert_record(rec(type="domain", pattern="*.ok.example", match="wildcard",
                            cells={"*": {"*": "allow"}}))
    # 文件类规则不该出现在 Linux 副本里（Linux 侧没有文件 ACL 段）
    store.upsert_record(rec(pattern="C:/locked", cells={"*": {"read": "deny"}}))

    counts = render.render_linux_copy(mode="*")

    assert counts == {"allowed_domains": 1, "blocked_domains": 2}
    data = _read_copy(env["linux_copy"])
    assert data == {"network": {"egress": {
        "allowed_domains": ["*.ok.example"], "blocked_domains": ["blocked.example", "*.blocked.example"],
    }}}


def test_render_linux_copy_warns_but_still_renders_on_unmanaged(env):
    """与 Windows 同一条不变式：出现未纳管条目 → 照常渲染 + 留痕。"""
    env["linux_copy"].write_text(yaml.safe_dump({
        "network": {"egress": {"allowed_domains": ["legacy.example"],
                               "blocked_domains": []}},
    }), encoding="utf-8")

    render.render_linux_copy(mode="*")

    assert _read_copy(env["linux_copy"])["network"]["egress"]["allowed_domains"] == []
    events = [
        json.loads(ln)
        for ln in env["audit"].read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert [e["kind"] for e in events] == [audit.AUDIT_RENDER_DROPPED]
    assert events[0]["target"] == "linux"


def test_panel_network_set_on_linux_writes_lists_not_copy(env, monkeypatch):
    """Linux 分支也收敛到名单：否则名单的域名规则永远到不了 Linux egress。"""
    monkeypatch.setattr(spr, "_is_windows", lambda: False)

    result = spr.set_sandbox_network_config(False, ["ok.example"], ["*.evil.example"])

    assert result["allow_domains"] == ["ok.example"]
    records = {r.pattern for r in store.get_security_lists()["user"] if r.type == "domain"}
    assert records == {"ok.example", "*.evil.example"}
    # Linux 副本由渲染产出（而不是 set 直接写）
    egress = _read_copy(env["linux_copy"])["network"]["egress"]
    assert egress == {"allowed_domains": ["ok.example", "*.ok.example"], "blocked_domains": ["*.evil.example"]}
    # 两份副本都由本次渲染统一产出（不做平台分支：哪台机器跑哪种沙箱，那份都是新的）
    assert _read_copy(env["copy"])["windows"]["network"]["egress"]["blocked_domains"] == [
        "*.evil.example"
    ]


def test_panel_network_set_on_linux_removal_propagates(env, monkeypatch):
    monkeypatch.setattr(spr, "_is_windows", lambda: False)
    spr.set_sandbox_network_config(False, ["a.example", "b.example"], [])
    spr.set_sandbox_network_config(False, ["a.example"], [])

    assert _read_copy(env["linux_copy"])["network"]["egress"]["allowed_domains"] == ["a.example", "*.a.example"]


def test_render_reports_non_absolute_paths(env):
    store.upsert_record(rec(pattern="*.pem", match="glob", cells={"*": {"read": "deny"}}))
    store.upsert_record(rec(pattern="C:/abs", cells={"*": {"read": "deny"}}))

    before = env["copy"].read_bytes() if env["copy"].exists() else None
    with pytest.raises(ValueError, match="absolute"):
        render.render_sandbox_copy(mode="*")
    if before is not None:
        assert env["copy"].read_bytes() == before
