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
