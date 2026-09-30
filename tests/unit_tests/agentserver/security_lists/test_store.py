# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.store 单测：CRUD、热读、损坏 fail-closed、云同步整批拒绝、迁移幂等。"""
from __future__ import annotations

import pytest
import yaml

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    DuplicateRecordError,
    SecurityListRecord,
    SecurityListsCorruptedError,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把 config.yaml 读写链重定向到临时文件（update_config/get_config 同文件）。"""
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: path)
    return path


def rec(**kw) -> SecurityListRecord:
    base = dict(type="file_path", pattern="C:/data", match="prefix",
                cells={"*": {"read": "allow"}})
    base.update(kw)
    return SecurityListRecord(**base)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def test_missing_section_returns_empty(cfg):
    result = store.get_security_lists()
    assert result == {
        "version": 3,
        "user": [],
        "cloud": {"sync_version": "", "synced_at": "", "records": []},
        "defaults": {},
    }


# ---------------------------------------------------------------------------
# v3：段版本闸门 + 兜底档 defaults
# ---------------------------------------------------------------------------


def test_set_and_get_defaults_roundtrip(cfg):
    store.set_defaults({"*": {"domain": "deny"}, "full_access": {"domain": "allow"}})
    assert store.get_security_lists()["defaults"] == {
        "*": {"domain": "deny"},
        "full_access": {"domain": "allow"},
    }
    # 写入即盖章段版本（v2 → v3 迁移闸门）
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["security_lists"]["version"] == 3


def test_set_defaults_validates_keyspace(cfg):
    with pytest.raises(ValueError):
        store.set_defaults({"*": {"domain": "block"}})
    assert store.get_security_lists()["defaults"] == {}   # 事务中止，未落盘


def test_legacy_section_without_version_is_readable(cfg):
    cfg.write_text(
        yaml.safe_dump({"security_lists": {"user": [], "cloud": {}}}), encoding="utf-8"
    )
    result = store.get_security_lists()
    assert result["defaults"] == {}


def test_future_version_fails_closed(cfg):
    cfg.write_text(
        yaml.safe_dump({"security_lists": {"version": 99, "user": []}}), encoding="utf-8"
    )
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


@pytest.mark.parametrize("version", ["three", 3.5, True, [3]])
def test_non_integer_version_fails_closed(cfg, version):
    cfg.write_text(
        yaml.safe_dump({"security_lists": {"version": version, "user": []}}),
        encoding="utf-8",
    )
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


def test_corrupted_defaults_fails_closed(cfg):
    cfg.write_text(
        yaml.safe_dump({"security_lists": {"defaults": {"*": {"domain": "block"}}}}),
        encoding="utf-8",
    )
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


def test_cloud_sync_can_set_defaults_and_keeps_them_when_absent(cfg):
    """云侧可下发兜底档；不下发时保留现值（不被静默清空）。"""
    store.cloud_sync(
        sync_version="v1", synced_at="t1", records=[],
        defaults={"*": {"domain": "deny"}},
    )
    assert store.get_defaults() == {"*": {"domain": "deny"}}

    store.cloud_sync(sync_version="v2", synced_at="t2", records=[])
    assert store.get_defaults() == {"*": {"domain": "deny"}}


def test_cloud_sync_rejects_invalid_defaults(cfg):
    with pytest.raises(ValueError):
        store.cloud_sync(
            sync_version="v1", synced_at="t1", records=[],
            defaults={"*": {"domain": "block"}},
        )
    assert store.get_defaults() == {}


def test_corrupted_yaml_fails_closed(cfg):
    cfg.write_text("security_lists: [unclosed\n", encoding="utf-8")
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


def test_corrupted_section_shape_fails_closed(cfg):
    cfg.write_text("security_lists: oops\n", encoding="utf-8")
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()
    # 写路径同样拒绝（不静默丢弃）
    with pytest.raises(SecurityListsCorruptedError):
        store.upsert_record(rec())


def test_corrupted_record_fails_closed(cfg):
    cfg.write_text(
        "security_lists:\n"
        "  user:\n"
        "    - {id: ul_1, type: domain, pattern: example.com, match: glob}\n",
        encoding="utf-8",
    )
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


# ---------------------------------------------------------------------------
# upsert / patch / delete
# ---------------------------------------------------------------------------


def test_upsert_create_and_hot_read(cfg):
    created = store.upsert_record(rec())
    assert created.id.startswith("ul_")
    assert created.created_at and created.updated_at
    result = store.get_security_lists()  # 写后 stamp 失效 → 立即读到（热更新链路）
    assert len(result["user"]) == 1
    assert result["user"][0].cells == {"*": {"read": "allow"}}

    store.upsert_record(rec(pattern="D:/other"))
    assert {r.pattern for r in store.get_security_lists()["user"]} == {"C:/data", "D:/other"}


def test_upsert_update_keeps_created_at(cfg):
    first = store.upsert_record(rec())
    updated = store.upsert_record(rec(id=first.id, cells={"*": {"read": "deny"}}))
    assert updated.created_at == first.created_at
    assert updated.updated_at >= first.created_at
    assert store.get_security_lists()["user"][0].cells == {"*": {"read": "deny"}}


def test_upsert_duplicate_object_rejected_without_write(cfg):
    store.upsert_record(rec())
    with pytest.raises(DuplicateRecordError):
        store.upsert_record(rec(id="ul_different"))
    assert len(store.get_security_lists()["user"]) == 1  # 事务中止，未落盘


def test_patch_cells_set_and_unset(cfg):
    created = store.upsert_record(rec())
    patched = store.patch_cells(created.id, set_={("default", "read"): "deny"})
    assert patched.cells == {"*": {"read": "allow"}, "default": {"read": "deny"}}

    patched = store.patch_cells(created.id, unset=[("*", "read")])
    assert patched.cells == {"default": {"read": "deny"}}

    # unset 清空整行后行被移除；cells 全空仍保留记录（无表态）
    patched = store.patch_cells(created.id, unset=[("default", "read")])
    assert patched.cells == {}
    assert len(store.get_security_lists()["user"]) == 1


def test_patch_cells_validates_key_space(cfg):
    created = store.upsert_record(rec())
    with pytest.raises(ValueError, match="不支持操作键"):
        store.patch_cells(created.id, set_={("*", "delete"): "deny"})
    with pytest.raises(ValueError, match="未知模式键"):
        store.patch_cells(created.id, set_={("strict", "read"): "deny"})
    with pytest.raises(ValueError, match="未知格子值"):
        store.patch_cells(created.id, set_={("*", "read"): "block"})


def test_patch_cells_missing_record(cfg):
    with pytest.raises(KeyError):
        store.patch_cells("ul_missing", set_={("*", "read"): "deny"})


def test_delete_record(cfg):
    created = store.upsert_record(rec())
    assert store.delete_record(created.id) is True
    assert store.get_security_lists()["user"] == []
    assert store.delete_record(created.id) is False


# ---------------------------------------------------------------------------
# cloud_sync
# ---------------------------------------------------------------------------


def test_cloud_sync_replaces_cloud_and_keeps_user(cfg):
    store.upsert_record(rec())
    applied = store.cloud_sync(
        sync_version="v1",
        synced_at="2026-09-28T10:00:00+00:00",
        records=[
            {"id": "cl_1", "type": "domain", "pattern": "example.com", "match": "exact",
             "cells": {"*": {"*": "deny"}}},
            {"id": "cl_2", "type": "command", "pattern": "rm -rf *", "match": "glob",
             "cells": {"*": {"*": "deny"}}},
        ],
    )
    assert applied == 2
    result = store.get_security_lists()
    assert len(result["user"]) == 1  # user 区不动
    cloud = result["cloud"]
    assert cloud["sync_version"] == "v1"
    assert cloud["synced_at"] == "2026-09-28T10:00:00+00:00"
    assert {r.id for r in cloud["records"]} == {"cl_1", "cl_2"}
    assert all(r.source == "cloud" for r in cloud["records"])

    # 再次同步 → 整区替换
    store.cloud_sync(sync_version="v2", synced_at="", records=[
        {"type": "domain", "pattern": "only.com", "match": "exact", "cells": {"*": {"*": "ask"}}},
    ])
    cloud = store.get_security_lists()["cloud"]
    assert cloud["sync_version"] == "v2"
    assert len(cloud["records"]) == 1
    assert cloud["records"][0].id  # 缺 id 自动生成


def test_cloud_sync_rejects_whole_batch_on_any_invalid(cfg):
    store.cloud_sync(sync_version="v1", synced_at="", records=[
        {"type": "domain", "pattern": "good.com", "match": "exact", "cells": {"*": {"*": "deny"}}},
    ])
    with pytest.raises(ValueError):
        store.cloud_sync(sync_version="v2", synced_at="", records=[
            {"type": "domain", "pattern": "ok.com", "match": "exact", "cells": {"*": {"*": "deny"}}},
            {"type": "domain", "pattern": "bad.com", "match": "glob", "cells": {}},  # 非法组合
        ])
    cloud = store.get_security_lists()["cloud"]  # 整批拒绝，旧数据原样
    assert cloud["sync_version"] == "v1"
    assert [r.pattern for r in cloud["records"]] == ["good.com"]


def test_cloud_sync_rejects_duplicate_within_batch(cfg):
    with pytest.raises(DuplicateRecordError):
        store.cloud_sync(sync_version="v1", synced_at="", records=[
            {"type": "domain", "pattern": "dup.com", "match": "exact", "cells": {"*": {"*": "deny"}}},
            {"type": "domain", "pattern": "dup.com", "match": "exact", "cells": {"*": {"*": "ask"}}},
        ])


# ---------------------------------------------------------------------------
# 沙箱副本一次性迁移
# ---------------------------------------------------------------------------


def write_copy(path):
    path.write_text(yaml.safe_dump({
        "windows": {
            "filesystem": {
                "allow_read": ["C:/data", "C:/shared"],
                "allow_write": ["C:/data"],
                "deny_read": ["C:/secret"],
                "deny_write": ["C:/data"],
            },
            "network": {
                "disable_all": True,
                "egress": {
                    "allowed_domains": ["example.com"],
                    "blocked_domains": ["*.evil.com"],
                },
            },
        },
    }, allow_unicode=True), encoding="utf-8")


def test_migrate_sandbox_copy_aggregates_and_keeps_copy(cfg, tmp_path):
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    write_copy(copy_path)

    migrated = store.migrate_sandbox_copy_once(copy_path)
    assert migrated == 5  # C:/data 四类条目合入一条

    records = {r.pattern: r for r in store.get_security_lists()["user"]}
    # 同对象 allow/deny 冲突 → deny 覆盖（deny 优先）
    assert records["C:/data"].cells == {"*": {"read": "allow", "write": "deny"}}
    assert records["C:/data"].match == "prefix"
    assert records["C:/data"].migrated_from == "sandbox_copy"
    assert records["C:/shared"].cells == {"*": {"read": "allow"}}
    assert records["C:/secret"].cells == {"*": {"read": "deny"}}
    assert records["example.com"].match == "exact"
    assert records["example.com"].cells == {"*": {"*": "allow"}}
    assert records["*.evil.com"].match == "wildcard"
    assert records["*.evil.com"].cells == {"*": {"*": "deny"}}

    # 副本用户段**保持原样**：它是 sandbox.files.set / sandbox.network.set 与
    # FileGuard 同步（sandbox.files.sync）的活配置，清空等于抹掉沙箱 ACL / egress。
    copy = yaml.safe_load(copy_path.read_text(encoding="utf-8"))
    fs = copy["windows"]["filesystem"]
    assert fs["allow_read"] == ["C:/data", "C:/shared"]
    assert fs["allow_write"] == ["C:/data"]
    assert fs["deny_read"] == ["C:/secret"]
    assert fs["deny_write"] == ["C:/data"]
    egress = copy["windows"]["network"]["egress"]
    assert egress == {
        "allowed_domains": ["example.com"],
        "blocked_domains": ["*.evil.com"],
    }
    assert copy["windows"]["network"]["disable_all"] is True

    # 幂等：第二次迁移返回 0，记录数不变
    assert store.migrate_sandbox_copy_once(copy_path) == 0
    assert len(store.get_security_lists()["user"]) == 5


def test_migrate_skips_objects_already_configured_by_user(cfg, tmp_path):
    store.upsert_record(rec(pattern="C:/data", cells={"*": {"exec": "deny"}}))
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    write_copy(copy_path)

    migrated = store.migrate_sandbox_copy_once(copy_path)
    assert migrated == 4  # C:/data 跳过（用户显式配置优先）
    records = {r.pattern: r for r in store.get_security_lists()["user"]}
    assert records["C:/data"].cells == {"*": {"exec": "deny"}}  # 用户配置原样
    assert records["C:/data"].migrated_from is None


def test_migrate_glob_path_match_kind(cfg, tmp_path):
    copy_path = tmp_path / "windows-policy.runtime.yaml"
    copy_path.write_text(yaml.safe_dump({
        "windows": {"filesystem": {"allow_read": ["C:/downloads/*.tmp"]}},
    }), encoding="utf-8")
    assert store.migrate_sandbox_copy_once(copy_path) == 1
    record = store.get_security_lists()["user"][0]
    assert record.match == "glob"


def test_migrate_no_copy_file(cfg, tmp_path):
    assert store.migrate_sandbox_copy_once(tmp_path / "nonexistent.yaml") == 0


# ---------------------------------------------------------------------------
# S3 写面收敛：legacy 段（net_guard.urls / file_guard.paths）一次性搬进 user 区
# ---------------------------------------------------------------------------


def _write_legacy_permissions(cfg, permissions):
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["permissions"] = permissions
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


LEGACY_PERMS = {
    "net_guard": {
        "enabled": True,
        "defaults": "deny",
        "urls": {"evil.example": "deny", "*.ok.example": "allow"},
    },
    "file_guard": {
        "enabled": True,
        "paths": [
            {"path": "C:/data", "read": "allow", "write": "ask", "match": "prefix",
             "mode": "default"},
            {"path": "C:/dl/**", "read": "deny", "match": "glob"},
        ],
    },
}


def test_migrate_legacy_copies_rules_and_keeps_legacy_sections(cfg):
    """搬的是**副本**：legacy 段不清空——强制点（core FileGuard/NetGuard）仍读它，
    删掉等于让路径层/宿主出口层掉规则（P3 逐跳校验我们的 rail 够不到）。"""
    _write_legacy_permissions(cfg, LEGACY_PERMS)

    result = store.migrate_legacy_once()

    assert result["created"] == 4 and result["candidates"] == 4
    records = {(r.type, r.pattern): r for r in store.get_security_lists()["user"]}

    evil = records[("domain", "evil.example")]
    assert evil.match == "exact" and evil.cells == {"*": {"*": "deny"}}
    assert evil.source == "user" and evil.migrated_from == "net_guard"
    assert records[("domain", "*.ok.example")].match == "wildcard"

    data = records[("file_path", "C:/data")]
    assert data.match == "prefix" and data.migrated_from == "file_guard"
    assert data.cells == {"default": {"read": "allow", "write": "ask"}}  # mode 格原样
    assert records[("file_path", "C:/dl/**")].match == "glob"

    # legacy 段原样保留
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["permissions"]["net_guard"]["urls"] == LEGACY_PERMS["net_guard"]["urls"]
    assert after["permissions"]["file_guard"]["paths"] == LEGACY_PERMS["file_guard"]["paths"]
    # net_guard.defaults（执行面兜底，出入管控侧语义）不搬——它没有对应记录
    assert after["permissions"]["net_guard"]["defaults"] == "deny"

    # 幂等：第二次返回 0（markers 已盖章）
    assert store.migrate_legacy_once()["created"] == 0
    assert len(store.get_security_lists()["user"]) == 4


def test_migrate_legacy_dry_run_writes_nothing(cfg):
    _write_legacy_permissions(cfg, LEGACY_PERMS)

    result = store.migrate_legacy_once(dry_run=True)

    assert result["candidates"] == 4 and result["created"] == 0
    assert {r.pattern for r in result["records"]} == {
        "evil.example", "*.ok.example", "C:/data", "C:/dl/**",
    }
    assert store.get_security_lists()["user"] == []
    assert store.get_security_lists()["version"] == 3


def test_migrate_legacy_skips_keys_already_owned_by_user(cfg):
    """用户已在名单里显式配过的操作对象不覆盖（用户配置优先）。"""
    store.upsert_record(rec(
        type="domain", pattern="evil.example", match="exact",
        cells={"*": {"*": "allow"}},
    ))
    _write_legacy_permissions(cfg, LEGACY_PERMS)

    result = store.migrate_legacy_once()

    assert result["created"] == 3 and result["skipped"] == 1
    records = {(r.type, r.pattern): r for r in store.get_security_lists()["user"]}
    assert records[("domain", "evil.example")].cells == {"*": {"*": "allow"}}
    assert records[("domain", "evil.example")].migrated_from is None


def test_migrate_legacy_disabled_sections_are_not_migrated(cfg):
    """面板总开关关掉（enabled=false）的段不搬——引擎整层不生效，搬了反而会拦。"""
    _write_legacy_permissions(cfg, {
        "net_guard": {"enabled": False, "urls": {"evil.example": "deny"}},
        "file_guard": {"enabled": False, "paths": [{"path": "C:/data", "read": "deny"}]},
    })

    result = store.migrate_legacy_once()

    assert result["candidates"] == 0 and store.get_security_lists()["user"] == []


def test_migrate_legacy_without_permissions_section_is_noop(cfg):
    assert store.migrate_legacy_once()["created"] == 0
    assert store.get_security_lists()["user"] == []


def test_migrate_legacy_sources_can_be_narrowed(cfg):
    """灰度：逐段放开（先只搬域名，观察一轮再搬路径）。"""
    _write_legacy_permissions(cfg, LEGACY_PERMS)

    result = store.migrate_legacy_once(sources=("net_guard",))

    assert result["created"] == 2
    records = {(r.type, r.pattern): r for r in store.get_security_lists()["user"]}
    assert set(records) == {("domain", "evil.example"), ("domain", "*.ok.example")}

    # 未被本次迁移的段仍可单独补搬（各自的 marker 独立）
    assert store.migrate_legacy_once(sources=("file_guard",))["created"] == 2


# ---------------------------------------------------------------------------
# 按来源整体替换（写面收敛：沙箱面板的 set 语义）
# ---------------------------------------------------------------------------


def test_replace_records_by_origin_replaces_only_its_own(cfg):
    """面板"整体替换"只能替换**它自己那份**——否则会把安全中心配的规则一起清掉。

    面板的 RPC 只有 set(allow, deny) 没有删除通道，"从面板移除一条"只能靠
    "不在新列表里"推断，所以必须知道"之前哪些是面板写的" → 用 migrated_from 做来源。
    """
    store.upsert_record(rec(pattern="C:/from-center", cells={"*": {"read": "deny"}}))
    store.replace_records_by_origin(
        origin="sandbox_panel", list_type="file_path",
        records=[rec(pattern="C:/panel-a", cells={"*": {"read": "allow"}})],
    )
    store.replace_records_by_origin(
        origin="sandbox_panel", list_type="file_path",
        records=[rec(pattern="C:/panel-b", cells={"*": {"read": "allow"}})],
    )

    records = {r.pattern: r for r in store.get_security_lists()["user"]}
    assert set(records) == {"C:/from-center", "C:/panel-b"}     # panel-a 被本次替换移除
    assert records["C:/from-center"].migrated_from is None      # 别家的没被动
    assert records["C:/panel-b"].migrated_from == "sandbox_panel"


def test_replace_records_by_origin_skips_conflicting_objects(cfg):
    """同操作对象已被别家占用 → 跳过并报出来（不夺权、不静默丢）。"""
    store.upsert_record(rec(pattern="C:/shared", cells={"*": {"read": "deny"}}))

    result = store.replace_records_by_origin(
        origin="sandbox_panel", list_type="file_path",
        records=[rec(pattern="C:/shared", cells={"*": {"read": "allow"}}),
                 rec(pattern="C:/own", cells={"*": {"read": "allow"}})],
    )

    assert result["created"] == 1 and result["skipped"] == ["C:/shared"]
    records = {r.pattern: r for r in store.get_security_lists()["user"]}
    assert records["C:/shared"].cells == {"*": {"read": "deny"}}   # 原样
    assert records["C:/shared"].migrated_from is None


def test_replace_records_by_origin_is_scoped_by_type(cfg):
    """只替换同类型的自己那份，不误伤另一类（面板域名集合不该动面板文件集合）。"""
    store.replace_records_by_origin(
        origin="sandbox_panel", list_type="domain",
        records=[rec(type="domain", pattern="panel.example", match="exact",
                     cells={"*": {"*": "deny"}})],
    )
    store.replace_records_by_origin(
        origin="sandbox_panel", list_type="file_path",
        records=[rec(pattern="C:/panel", cells={"*": {"read": "allow"}})],
    )

    records = {(r.type, r.pattern) for r in store.get_security_lists()["user"]}
    assert records == {("domain", "panel.example"), ("file_path", "C:/panel")}


def test_replace_records_by_origin_empty_clears_own(cfg):
    store.replace_records_by_origin(
        origin="sandbox_panel", list_type="domain",
        records=[rec(type="domain", pattern="panel.example", match="exact",
                     cells={"*": {"*": "deny"}})],
    )
    store.replace_records_by_origin(origin="sandbox_panel", list_type="domain", records=[])
    assert store.get_security_lists()["user"] == []


def test_migrate_legacy_unknown_source_rejected(cfg):
    with pytest.raises(ValueError, match="未知迁移来源"):
        store.migrate_legacy_once(sources=("approval_overrides",))


def test_migrate_legacy_merges_duplicate_objects_strictest_wins(cfg):
    """legacy 同对象重复条目（手改配置/历史脏数据的常见形态）合入一条记录。

    user 区 ``(type, pattern, match)`` 唯一，不合并就会撞唯一性校验 → 整批事务中止、
    迁移直接失败。合并规则取**最严**（安全名单的默认取向：宁可更紧不可更松）。
    """
    _write_legacy_permissions(cfg, {
        "file_guard": {"enabled": True, "paths": [
            {"path": "C:/data", "read": "allow", "match": "prefix"},
            {"path": "C:/data", "read": "deny", "write": "ask", "match": "prefix"},
        ]},
    })

    result = store.migrate_legacy_once()

    assert result["created"] == 1
    record = store.get_security_lists()["user"][0]
    assert record.cells == {"*": {"read": "deny", "write": "ask"}}
