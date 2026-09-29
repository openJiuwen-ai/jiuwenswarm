# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists 段存储层（config.yaml CRUD）。

- 读：:func:`jiuwenswarm.common.config.get_config`（mtime stamp 缓存，
  写后 stamp 失效 → 下次求值即新名单，热更新链路）；
- 写：:func:`jiuwenswarm.common.config.update_config`（portalocker 跨进程锁
  + 线程锁，load→mutate→dump 原子临界区）。**mutator 内禁止再调任何走
  update_config 的函数**（锁不可重入）；锁超时/IO 异常原样上抛
  （rail 按 fail-closed 处理，RPC 映射 500+告警）。

config.yaml 段结构::

    security_lists:
      user: []            # SecurityListRecord 列表（cells 稀疏矩阵）
      cloud:
        sync_version: ""
        synced_at: ""
        records: []
      migrations:         # 一次性迁移标记（内部）
        sandbox_copy: "<iso time>"
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.common.config import get_config, update_config

from .models import (
    SecurityListRecord,
    SecurityListsCorruptedError,
    has_glob_chars,
    new_record_id,
    record_from_dict,
    record_to_dict,
    utc_now_iso,
    validate_record,
)

logger = logging.getLogger(__name__)

_SECTION = "security_lists"
_MIGRATION_SANDBOX_COPY = "sandbox_copy"

#: 沙箱运行时副本文件名（与 server/sandbox_policy_render.py:_RUNTIME_COPY_NAME 保持一致）
_RUNTIME_COPY_NAME = "windows-policy.runtime.yaml"


def _empty_cloud() -> dict[str, Any]:
    return {"sync_version": "", "synced_at": "", "records": []}


def _empty_section() -> dict[str, Any]:
    return {"user": [], "cloud": _empty_cloud()}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _parse_records(raw_list: Any, *, default_source: str) -> list[SecurityListRecord]:
    """严格解析记录列表：畸形即 ValueError（调用方包成 CorruptedError）。"""
    if not isinstance(raw_list, list):
        raise ValueError(f"records 须为列表: {type(raw_list).__name__}")
    records: list[SecurityListRecord] = []
    for raw in raw_list:
        rec = record_from_dict(raw, default_source=default_source)
        validate_record(rec, existing=records)
        records.append(rec)
    return records


def _parse_section(section: Any) -> dict[str, Any]:
    """严格解析 security_lists 段（未知键如 migrations 忽略）。"""
    if not isinstance(section, dict):
        raise ValueError(f"security_lists 段须为映射: {type(section).__name__}")
    user = _parse_records(section.get("user") or [], default_source="user")
    cloud_raw = section.get("cloud") or {}
    if not isinstance(cloud_raw, dict):
        raise ValueError(f"security_lists.cloud 须为映射: {type(cloud_raw).__name__}")
    cloud_records = _parse_records(cloud_raw.get("records") or [], default_source="cloud")
    return {
        "user": user,
        "cloud": {
            "sync_version": str(cloud_raw.get("sync_version") or ""),
            "synced_at": str(cloud_raw.get("synced_at") or ""),
            "records": cloud_records,
        },
    }


def get_security_lists() -> dict[str, Any]:
    """读 config.yaml ``security_lists`` 段（热读）。

    段缺失 → 空结构；段/记录解析异常 → :class:`SecurityListsCorruptedError`
    （供 rail fail-closed）。
    """
    try:
        config = get_config()
    except Exception as exc:  # yaml 解析失败等（整个 config 不可读）
        raise SecurityListsCorruptedError(f"config.yaml 解析失败: {exc}") from exc
    section = config.get(_SECTION)
    if section is None:
        return _empty_section()
    try:
        return _parse_section(section)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SecurityListsCorruptedError(f"security_lists 段解析失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 写入（全部经 update_config 事务）
# ---------------------------------------------------------------------------


def _ensure_section(data: dict[str, Any]) -> dict[str, Any]:
    """在待写数据上取/建 security_lists 段（mutator 内调用）。

    段已存在但结构畸形 → :class:`SecurityListsCorruptedError`（与读路径一致，
    不静默丢弃用户数据）；缺键补齐。
    """
    section = data.get(_SECTION)
    if section is None:
        section = _empty_section()
        data[_SECTION] = section
        return section
    if not isinstance(section, dict):
        raise SecurityListsCorruptedError(f"security_lists 段须为映射: {type(section).__name__}")
    if "user" in section and not isinstance(section["user"], list):
        raise SecurityListsCorruptedError("security_lists.user 须为列表")
    section.setdefault("user", [])
    if "cloud" in section and not isinstance(section["cloud"], dict):
        raise SecurityListsCorruptedError("security_lists.cloud 须为映射")
    section.setdefault("cloud", _empty_cloud())
    return section


def _parse_existing_user(section: dict[str, Any]) -> list[SecurityListRecord]:
    """mutator 内解析存量 user 记录；损坏则抛 CorruptedError（事务中止，不落盘）。"""
    try:
        return _parse_records(section.get("user") or [], default_source="user")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SecurityListsCorruptedError(f"security_lists.user 解析失败，拒绝写入: {exc}") from exc


def upsert_record(rec: SecurityListRecord) -> SecurityListRecord:
    """新建/更新 user 区记录。

    id 冲突走更新（保留 created_at，刷 updated_at）；否则新建（自动补 id
    与时间戳）。唯一性冲突抛 :class:`DuplicateRecordError`。
    """
    now = utc_now_iso()

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = _ensure_section(data)
        existing = _parse_existing_user(section)
        if not rec.id:
            rec.id = new_record_id()
        rec.source = "user"
        validate_record(rec, existing=existing)
        user_raw = section["user"]
        for i, old in enumerate(existing):
            if old.id == rec.id:
                rec.created_at = rec.created_at or old.created_at or now
                rec.updated_at = now
                user_raw[i] = record_to_dict(rec)
                return data
        rec.created_at = rec.created_at or now
        rec.updated_at = now
        user_raw.append(record_to_dict(rec))
        return data

    update_config(_mutate)
    logger.info("security_lists upsert: id=%s type=%s pattern=%r", rec.id, rec.type, rec.pattern)
    return rec


def patch_cells(
    record_id: str,
    *,
    set_: dict[tuple[str, str], str] | None = None,
    unset: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
) -> SecurityListRecord:
    """格子级增改删（``set_[(mode, op)] = action``；``unset`` 删除格子）。

    unset 后 cells 为空仍保留记录（无表态记录）。记录不存在抛 :class:`KeyError`。
    """
    set_ = set_ or {}
    now = utc_now_iso()
    result: list[SecurityListRecord] = []

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = _ensure_section(data)
        existing = _parse_existing_user(section)
        for i, rec in enumerate(existing):
            if rec.id != record_id:
                continue
            for (mode, op), action in set_.items():
                rec.cells.setdefault(mode, {})[op] = action
            for mode, op in unset:
                row = rec.cells.get(mode)
                if isinstance(row, dict):
                    row.pop(op, None)
                    if not row:
                        rec.cells.pop(mode, None)
            rec.updated_at = now
            # 全量校验（含新增格子的键空间/取值）
            validate_record(rec, existing=existing)
            section["user"][i] = record_to_dict(rec)
            result.append(rec)
            return data
        raise KeyError(f"记录不存在: {record_id}")

    update_config(_mutate)
    logger.info("security_lists cells.patch: id=%s set=%d unset=%d", record_id, len(set_), len(unset))
    return result[0]


def delete_record(record_id: str) -> bool:
    """删除 user 区记录，返回是否命中。"""
    found = False

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal found
        section = _ensure_section(data)
        user_raw = section["user"]
        kept = [r for r in user_raw if not (isinstance(r, dict) and r.get("id") == record_id)]
        found = len(kept) != len(user_raw)
        section["user"] = kept
        return data

    update_config(_mutate)
    if found:
        logger.info("security_lists delete: id=%s", record_id)
    return found


def cloud_sync(*, sync_version: str, records: list[dict[str, Any]], synced_at: str) -> int:
    """cloud 区整区替换（user 区不动）。

    records 逐条校验，**任一非法整批拒绝**（抛 :class:`ValueError`，RPC 映射 400）；
    批次内 ``(type, pattern, match)`` 重复同样整批拒绝。返回应用条数。
    """
    parsed: list[SecurityListRecord] = []
    for raw in records:
        rec = record_from_dict(raw, default_source="cloud")
        rec.source = "cloud"
        validate_record(rec, existing=parsed)
        parsed.append(rec)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = _ensure_section(data)
        now = utc_now_iso()
        for rec in parsed:
            if not rec.id:
                rec.id = new_record_id()
            rec.created_at = rec.created_at or now
            rec.updated_at = now
        section["cloud"] = {
            "sync_version": str(sync_version or ""),
            "synced_at": str(synced_at or ""),
            "records": [record_to_dict(r) for r in parsed],
        }
        return data

    update_config(_mutate)
    logger.info("security_lists cloud.sync: version=%r applied=%d", sync_version, len(parsed))
    return len(parsed)


# ---------------------------------------------------------------------------
# 一次性迁移：沙箱运行时副本 → user 区聚合记录
# ---------------------------------------------------------------------------


def _default_copy_path() -> Path:
    """沙箱运行时副本默认落点（与 sandbox_policy_render._runtime_copy_path 一致）。"""
    from jiuwenswarm.common.utils import get_config_dir  # lazy import，避免环

    return get_config_dir() / _RUNTIME_COPY_NAME


def _migration_marker(section: dict[str, Any]) -> str | None:
    migrations = section.get("migrations")
    if not isinstance(migrations, dict):
        return None
    marker = migrations.get(_MIGRATION_SANDBOX_COPY)
    return str(marker) if marker else None


def _copy_file_rules(copy: dict[str, Any]) -> dict[str, list[str]]:
    """从副本提取四类文件名单（缺字段按空表）。"""
    win = copy.get("windows") if isinstance(copy, dict) else None
    fs = win.get("filesystem") if isinstance(win, dict) else None
    if not isinstance(fs, dict):
        return {"allow_read": [], "allow_write": [], "deny_read": [], "deny_write": []}
    return {
        key: [str(v) for v in (fs.get(key) or []) if str(v).strip()]
        for key in ("allow_read", "allow_write", "deny_read", "deny_write")
    }


def _copy_domain_rules(copy: dict[str, Any]) -> dict[str, list[str]]:
    """从副本提取网络域名黑/白名单。"""
    win = copy.get("windows") if isinstance(copy, dict) else None
    net = win.get("network") if isinstance(win, dict) else None
    egress = net.get("egress") if isinstance(net, dict) else None
    if not isinstance(egress, dict):
        return {"allowed_domains": [], "blocked_domains": []}
    return {
        key: [str(v) for v in (egress.get(key) or []) if str(v).strip()]
        for key in ("allowed_domains", "blocked_domains")
    }


def _build_migrated_records(copy: dict[str, Any], now: str) -> list[SecurityListRecord]:
    """副本名单 → 聚合记录（同一操作对象多名单条目合入一条 cells；冲突 deny 优先）。

    - 文件路径：含 glob 元字符 → match=glob，否则 prefix（对齐沙箱 ACL 目录语义）；
    - 域名：``*.`` 开头 → wildcard，否则 exact；
    - 格子全部落在 ``"*"`` 通用格（模式无关）；``migrated_from="sandbox_copy"``。
    """
    merged: dict[tuple[str, str, str], SecurityListRecord] = {}

    def _file_rec(path: str) -> SecurityListRecord:
        match = "glob" if has_glob_chars(path) else "prefix"
        key = ("file_path", path, match)
        rec = merged.get(key)
        if rec is None:
            rec = SecurityListRecord(
                id=new_record_id(),
                type="file_path",
                pattern=path,
                match=match,
                cells={},
                created_at=now,
                updated_at=now,
                source="user",
                migrated_from=_MIGRATION_SANDBOX_COPY,
            )
            merged[key] = rec
        return rec

    def _domain_rec(domain: str) -> SecurityListRecord:
        match = "wildcard" if domain.startswith("*.") else "exact"
        key = ("domain", domain, match)
        rec = merged.get(key)
        if rec is None:
            rec = SecurityListRecord(
                id=new_record_id(),
                type="domain",
                pattern=domain,
                match=match,
                cells={},
                created_at=now,
                updated_at=now,
                source="user",
                migrated_from=_MIGRATION_SANDBOX_COPY,
            )
            merged[key] = rec
        return rec

    files = _copy_file_rules(copy)
    # 先 allow 后 deny：同对象冲突时 deny 覆盖（deny 优先）
    for path in files["allow_read"]:
        _file_rec(path).cells.setdefault("*", {})["read"] = "allow"
    for path in files["allow_write"]:
        _file_rec(path).cells.setdefault("*", {})["write"] = "allow"
    for path in files["deny_read"]:
        _file_rec(path).cells.setdefault("*", {})["read"] = "deny"
    for path in files["deny_write"]:
        _file_rec(path).cells.setdefault("*", {})["write"] = "deny"

    domains = _copy_domain_rules(copy)
    for domain in domains["allowed_domains"]:
        _domain_rec(domain).cells.setdefault("*", {})["*"] = "allow"
    for domain in domains["blocked_domains"]:
        _domain_rec(domain).cells.setdefault("*", {})["*"] = "deny"

    return list(merged.values())


def _clear_copy_user_sections(copy_path: Path, copy: dict[str, Any]) -> None:
    """清空副本用户名单段（``disable_all`` 总开关保留不动）；原子写回。"""
    win = copy.get("windows")
    if isinstance(win, dict):
        fs = win.get("filesystem")
        if isinstance(fs, dict):
            for key in ("allow_read", "allow_write", "deny_read", "deny_write"):
                fs[key] = []
        net = win.get("network")
        if isinstance(net, dict):
            egress = net.get("egress")
            if isinstance(egress, dict):
                egress["allowed_domains"] = []
                egress["blocked_domains"] = []
    tmp = copy_path.with_suffix(copy_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(copy, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, copy_path)


def migrate_sandbox_copy_once(copy_path: Path | None = None) -> int:
    """windows-policy.runtime.yaml 用户副本 → user 区聚合记录（幂等）。

    已迁移（``security_lists.migrations.sandbox_copy`` 有标记）→ 返回 0。
    与存量 user 记录同 ``(type, pattern, match)`` 的迁移条目跳过（用户显式
    配置优先）。迁移成功后清空副本用户名单段。返回新建记录条数。
    """
    copy_path = Path(copy_path) if copy_path is not None else _default_copy_path()
    if not copy_path.is_file():
        return 0
    try:
        copy = yaml.safe_load(copy_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读沙箱副本 %s 失败，跳过迁移: %s", copy_path, exc)
        return 0
    if not isinstance(copy, dict):
        return 0

    now = utc_now_iso()
    candidates = _build_migrated_records(copy, now)
    migrated = 0
    already_done = False

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal migrated, already_done
        section = _ensure_section(data)
        if _migration_marker(section):
            already_done = True
            return None  # 无变更不落盘（update_config 对 None 跳过写）
        existing = _parse_existing_user(section)
        user_raw = section["user"]
        occupied = {(r.type, r.pattern, r.match) for r in existing}
        for rec in candidates:
            key = (rec.type, rec.pattern, rec.match)
            if key in occupied:
                logger.info("迁移跳过（用户已配置同操作对象）: %s %r", rec.type, rec.pattern)
                continue
            validate_record(rec, existing=existing)
            user_raw.append(record_to_dict(rec))
            existing.append(rec)
            occupied.add(key)
            migrated += 1
        section.setdefault("migrations", {})[_MIGRATION_SANDBOX_COPY] = now
        return data

    update_config(_mutate)
    if already_done:
        return 0
    # 标记已落盘后再清副本：即使清副本失败，重启也不会重复迁移（幂等）。
    if candidates:
        try:
            _clear_copy_user_sections(copy_path, copy)
        except OSError as exc:
            logger.warning("清空沙箱副本用户段失败（迁移标记已写，不会重复迁移）: %s", exc)
    logger.info("沙箱副本迁移完成: 新建 %d 条名单记录", migrated)
    return migrated
