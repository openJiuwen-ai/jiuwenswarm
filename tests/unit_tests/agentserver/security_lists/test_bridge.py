# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""S2 回归：名单 domain 规则回流到 core ``net_guard`` 强制点。

覆盖三件事：
1. 语义映射（exact → 裸域 + ``*.``，ask → deny，同键取严）；
2. 边界（net_guard 缺失/关闭不动、段损坏不动、不原地改调用方 dict）；
3. 挂载点（P3 发布路径确实拿到合并后的 urls）。
"""
from __future__ import annotations

import pytest
import yaml

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.bridge import (
    merge_domain_rules_into_net_guard,
    permissions_for_enforcement,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: path)
    return path


def write_lists(cfg, user=(), cloud=()):
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["security_lists"] = {
        "version": 3,
        "user": list(user),
        "cloud": {"sync_version": "v1", "synced_at": "", "records": list(cloud)},
        "defaults": {},
    }
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def record(pattern, action, *, match="exact", list_type="domain", enabled=True,
           cells=None, **extra):
    return {
        "id": f"ul_{pattern}", "type": list_type, "pattern": pattern, "match": match,
        "enabled": enabled,
        "cells": cells if cells is not None else {"*": {"*": action}},
        **extra,
    }


def ng_perms(urls=None, *, enabled=True):
    section = {"enabled": enabled, "defaults": "allow", "urls": dict(urls or {})}
    return {"net_guard": section}


# ---------------------------------------------------------------------------
# 语义映射
# ---------------------------------------------------------------------------


def test_exact_domain_expands_to_bare_and_subdomain(cfg):
    """名单 ``exact`` 是「裸域 + 子域」，net_guard 的裸 pattern 全串匹配只命中裸域。

    只注入裸域那一条，``sub.evil.example`` 会在宿主出口漏掉，而我们的 rail 照拦
    ——正是 S2 要消除的"护栏拦得住、出口漏"的不一致。
    """
    write_lists(cfg, user=[record("evil.example", "deny")])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"] == {
        "evil.example": "deny",
        "*.evil.example": "deny",
    }


def test_wildcard_domain_maps_to_single_key(cfg):
    write_lists(cfg, user=[record("*.ok.example", "allow", match="wildcard")])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"] == {"*.ok.example": "allow"}


def test_ask_becomes_deny_at_host_exit(cfg):
    """宿主出口层没有用户可问（只有 block / pass），ask 按 fail-closed 落 deny。

    与 ``api.check_*_static`` 的契约一致（对齐稿 §2）。
    """
    write_lists(cfg, user=[record("maybe.example", "ask")])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"]["maybe.example"] == "deny"


def test_cloud_records_are_merged_too(cfg):
    """云侧下发的恶意域名必须拦在宿主出口——这是它最主要的用途。"""
    write_lists(cfg, cloud=[record("*.malware.example", "deny", match="wildcard",
                                   source="cloud")])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"] == {"*.malware.example": "deny"}


def test_conflict_takes_the_stricter_never_loosens(cfg):
    """与既有 urls 同键冲突取严：回流只可能更紧，不会把别人的 deny 放宽成 allow。"""
    write_lists(cfg, user=[record("evil.example", "allow")])

    merged = merge_domain_rules_into_net_guard(
        ng_perms({"evil.example": "deny", "*.evil.example": "deny"})
    )

    assert merged["net_guard"]["urls"] == {
        "evil.example": "deny", "*.evil.example": "deny",
    }


def test_existing_invalid_value_is_replaced(cfg):
    """既有值非法（core 会跳过它）→ 我们的动作直接落上，不因"同键"而放弃。"""
    write_lists(cfg, user=[record("evil.example", "deny")])

    merged = merge_domain_rules_into_net_guard(ng_perms({"evil.example": "block"}))

    assert merged["net_guard"]["urls"]["evil.example"] == "deny"


def test_missing_general_cell_is_not_merged(cfg):
    """只配了模式特化格的记录不进 net_guard：它是进程级全局策略，没有模式概念。

    硬套某个档位的取值会把"仅默认档禁止"放大成"所有档位禁止"。
    """
    write_lists(cfg, user=[record(
        "mode-only.example", None, cells={"default": {"*": "deny"}},
    )])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"] == {}


def test_disabled_and_non_domain_records_are_skipped(cfg):
    write_lists(cfg, user=[
        record("off.example", "deny", enabled=False),
        record("C:/data", "deny", match="prefix", list_type="file_path"),
        record("bash", "deny", match="exact", list_type="tool"),
    ])

    merged = merge_domain_rules_into_net_guard(ng_perms())

    assert merged["net_guard"]["urls"] == {}


# ---------------------------------------------------------------------------
# 边界：绝不新建 / 绝不启用 net_guard
# ---------------------------------------------------------------------------


def test_missing_net_guard_section_is_left_absent(cfg):
    """net_guard 段缺失 = 该强制点未启用。凭空建段等于替他们打开强制，是行为突变。"""
    write_lists(cfg, user=[record("evil.example", "deny")])

    merged = merge_domain_rules_into_net_guard({"enabled": True})

    assert "net_guard" not in merged


def test_disabled_net_guard_is_not_touched(cfg):
    """net_guard.enabled=false → P1/P3 整层不生效（他们的开关），回流也不该绕过它。"""
    write_lists(cfg, user=[record("evil.example", "deny")])

    merged = merge_domain_rules_into_net_guard(ng_perms({}, enabled=False))

    assert merged["net_guard"]["urls"] == {}


def test_corrupted_section_returns_permissions_unchanged(cfg):
    """段损坏 → 保持原状并告警（rail 那一侧已 fail-closed 拒绝所有调用，不叠加放大）。"""
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["security_lists"] = "not-a-mapping"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    perms = ng_perms({"keep.example": "deny"})

    merged = merge_domain_rules_into_net_guard(perms)

    assert merged["net_guard"]["urls"] == {"keep.example": "deny"}


def test_caller_dict_is_not_mutated_in_place(cfg):
    """调用方的 config 是共享对象，回流必须产出新 dict（尤其 net_guard 子段）。"""
    write_lists(cfg, user=[record("evil.example", "deny")])
    perms = ng_perms({"keep.example": "deny"})

    merged = merge_domain_rules_into_net_guard(perms)

    assert merged is not perms
    assert perms["net_guard"]["urls"] == {"keep.example": "deny"}
    assert merged["net_guard"]["urls"]["evil.example"] == "deny"


def test_permissions_for_enforcement_reads_config_section(cfg):
    write_lists(cfg, user=[record("evil.example", "deny")])
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["permissions"] = ng_perms({"keep.example": "deny"})
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    perms = permissions_for_enforcement(config_mod.get_config())

    assert perms["net_guard"]["urls"] == {
        "keep.example": "deny", "evil.example": "deny", "*.evil.example": "deny",
    }


def test_permissions_for_enforcement_without_permissions_section(cfg):
    assert permissions_for_enforcement({}) == {}
    assert permissions_for_enforcement(None) == {}


# ---------------------------------------------------------------------------
# 挂载点：P3 发布路径必须拿到合并后的 urls
# ---------------------------------------------------------------------------


def test_publish_host_exit_policy_receives_merged_rules(cfg, monkeypatch):
    """P3 是进程级发布：只在这一处合并，名单改域名才能不经重启就管到宿主出口。"""
    import openjiuwen.harness.security.outbound as outbound

    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc

    write_lists(cfg, user=[record("evil.example", "deny")])
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["permissions"] = ng_perms()
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    captured = {}
    monkeypatch.setattr(
        outbound, "publish_host_exit_policy", lambda perms: captured.update(perms or {})
    )

    permissions_config_rpc.publish_host_exit_policy_from_config()

    urls = captured["net_guard"]["urls"]
    assert urls["evil.example"] == "deny" and urls["*.evil.example"] == "deny"


def test_merged_rules_reach_the_real_net_guard_checker(cfg):
    """端到端：合并结果交给 core 真实的 ``NetGuardChecker``，裸域与**子域**都拦得住。

    这条是"``exact`` 必须展开成两条"的直接验收——只注入裸域时子域断言会红。
    """
    from openjiuwen.harness.security.permission_engine.core import (
        prepare_permissions_for_engine,
    )
    from openjiuwen.harness.security.permission_engine.netguard.net_guard import (
        build_net_guard_checker,
    )

    write_lists(cfg, user=[record("evil.example", "deny")])
    merged = merge_domain_rules_into_net_guard(ng_perms())

    checker = build_net_guard_checker(prepare_permissions_for_engine(merged))

    assert checker is not None
    assert checker.check_url("https://evil.example/a") is not None       # 裸域
    assert checker.check_url("https://sub.evil.example/a") is not None   # 子域（关键）
    assert checker.check_url("https://ok.example/a") is None             # 无关域名放行


def test_store_write_then_merge_reflects_new_rule(cfg):
    """热更新链路：写到 store 之后，同一次回流就能看到新规则（不做进程级缓存）。"""
    assert merge_domain_rules_into_net_guard(ng_perms())["net_guard"]["urls"] == {}

    store.upsert_record(store.record_from_dict(record("later.example", "deny")))

    assert merge_domain_rules_into_net_guard(ng_perms())["net_guard"]["urls"] == {
        "later.example": "deny", "*.later.example": "deny",
    }
