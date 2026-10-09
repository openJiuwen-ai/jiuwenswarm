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
from pathlib import Path
from typing import Any, Mapping

import yaml

from jiuwenswarm.common.config import get_config, update_config

from .models import (
    DuplicateRecordError,
    SECURITY_LISTS_VERSION,
    DEFAULT_TYPE_KEYS,
    MODE_KEYS,
    SecurityListRecord,
    SecurityListsCorruptedError,
    has_glob_chars,
    new_record_id,
    record_from_dict,
    record_to_dict,
    resolve_default,
    utc_now_iso,
    validate_defaults,
    validate_record,
)

logger = logging.getLogger(__name__)

_SECTION = "security_lists"
_MIGRATION_SANDBOX_COPY = "sandbox_copy"

#: 特性开关式的 legacy 段迁移来源（写面收敛，S3）。
#: 键 = ``permissions`` 下的段名，也是 ``migrated_from`` 与 ``migrations`` 标记名。
#: ``approval_overrides`` 刻意**不在**其中：它是审批流自己产生的通道（只能由
#: "永久/会话记住"写入），没有与新面板争夺同一编辑面的问题，且搬进 user 区会
#: 把它从 ``user_approval`` 层降到 ``user`` 层（allow 可能被同层 ask 压过）。
_LEGACY_SOURCES: tuple[str, ...] = ("net_guard", "file_guard")

#: 段内无 ``version`` 键时的兼容版本（本版之前的写法：无 defaults 段）
_LEGACY_VERSION = 2

#: 沙箱运行时副本文件名（与 server/sandbox_policy_render.py:_RUNTIME_COPY_NAME 保持一致）
_RUNTIME_COPY_NAME = "windows-policy.runtime.yaml"


def _empty_cloud() -> dict[str, Any]:
    return {"sync_version": "", "synced_at": "", "records": []}


def _empty_section() -> dict[str, Any]:
    return {
        "version": SECURITY_LISTS_VERSION,
        "user": [],
        "cloud": _empty_cloud(),
        "defaults": {},
    }


def _parse_version(raw: Any) -> int:
    """段版本闸门：缺省 → :data:`_LEGACY_VERSION`（兼容读）；非法/过新 → :class:`ValueError`。

    过新（``> SECURITY_LISTS_VERSION``）必须拒绝而不是尽力解析——按旧结构读新数据
    会静默曲解规则语义，宁可 fail-closed。
    """
    if raw is None:
        return _LEGACY_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"security_lists.version 须为整数: {raw!r}")
    if raw > SECURITY_LISTS_VERSION:
        raise ValueError(
            f"security_lists.version={raw} 高于本版支持（{SECURITY_LISTS_VERSION}），拒绝按旧结构解析"
        )
    return raw


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
    version = _parse_version(section.get("version"))
    user = _parse_records(section.get("user", []), default_source="user")
    cloud_raw = section.get("cloud", {})
    if not isinstance(cloud_raw, dict):
        raise ValueError(f"security_lists.cloud 须为映射: {type(cloud_raw).__name__}")
    cloud_records = _parse_records(cloud_raw.get("records", []), default_source="cloud")
    defaults_raw = section.get("defaults", {})
    validate_defaults(defaults_raw)
    cloud_defaults = cloud_raw.get("defaults", {})
    validate_defaults(cloud_defaults)
    return {
        "version": version,
        "user": user,
        "cloud": {
            "sync_version": str(cloud_raw.get("sync_version") or ""),
            "synced_at": str(cloud_raw.get("synced_at") or ""),
            "records": cloud_records,
            **({"defaults": cloud_defaults} if cloud_defaults else {}),
        },
        "defaults": {
            str(mode): {str(list_type): str(action) for list_type, action in row.items()}
            for mode, row in defaults_raw.items()
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


def _ensure_section(data: dict[str, Any], *, bootstrap_legacy: bool = True) -> dict[str, Any]:
    """在待写数据上取/建 security_lists 段（mutator 内调用）。

    段已存在但结构畸形 → :class:`SecurityListsCorruptedError`（与读路径一致，
    不静默丢弃用户数据）；缺键补齐。
    """
    section = data.get(_SECTION)
    if section is None:
        section = _empty_section()
        data[_SECTION] = section
        if bootstrap_legacy:
            from .legacy_compat import bootstrap
            bootstrap(data, section)
        return section
    if not isinstance(section, dict):
        raise SecurityListsCorruptedError(f"security_lists 段须为映射: {type(section).__name__}")
    if "user" in section and not isinstance(section["user"], list):
        raise SecurityListsCorruptedError("security_lists.user 须为列表")
    section.setdefault("user", [])
    if "cloud" in section and not isinstance(section["cloud"], dict):
        raise SecurityListsCorruptedError("security_lists.cloud 须为映射")
    section.setdefault("cloud", _empty_cloud())
    # 非法/过新的存量版本先拒（事务中止，不覆盖用户数据）
    if section.get("version") is not None:
        try:
            _parse_version(section.get("version"))
        except ValueError as exc:
            raise SecurityListsCorruptedError(f"security_lists.version 非法，拒绝写入: {exc}") from exc
    # 存量 defaults 非法同样拒写（与读路径一致，不静默丢弃）
    try:
        validate_defaults(section.get("defaults", {}))
        validate_defaults(section["cloud"].get("defaults", {}))
    except ValueError as exc:
        raise SecurityListsCorruptedError(f"security_lists.defaults 非法，拒绝写入: {exc}") from exc
    section.setdefault("defaults", {})
    # 写入即盖章当前结构版本（v2 → v3 迁移闸门；defaults 缺失＝保持现状）
    section["version"] = SECURITY_LISTS_VERSION
    if bootstrap_legacy:
        from .legacy_compat import bootstrap
        bootstrap(data, section)
    return section


def get_defaults() -> dict[str, Any]:
    """读生效兜底档；用户格（包括通用格）优先于云侧格，物理存储隔离。"""
    lists = get_security_lists()
    user = lists.get("defaults") or {}
    cloud = lists["cloud"].get("defaults") or {}
    if not user or not cloud:
        return {mode: dict(row) for mode, row in (user or cloud).items()}
    merged: dict[str, Any] = {}
    for mode in MODE_KEYS:
        row = {}
        for list_type in DEFAULT_TYPE_KEYS:
            action = resolve_default(user, mode, list_type)
            if action is None:
                action = resolve_default(cloud, mode, list_type)
            if action is not None:
                row[list_type] = action
        if row:
            merged[mode] = row
    return merged


def set_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    """整体替换兜底档（键空间校验；非法抛 :class:`ValueError` 且不落盘）。

    白名单模式即 ``{"*": {"domain": "deny"}}``——"未列出即拒"。
    """
    validate_defaults(defaults)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = _ensure_section(data)
        section["defaults"] = {
            str(mode): {str(list_type): str(action) for list_type, action in row.items()}
            for mode, row in defaults.items()
        }
        return data

    update_config(_mutate)
    logger.info("security_lists defaults set: modes=%s", sorted(str(m) for m in defaults))
    return defaults


def _parse_existing_user(section: dict[str, Any]) -> list[SecurityListRecord]:
    """mutator 内解析存量 user 记录；损坏则抛 CorruptedError（事务中止，不落盘）。"""
    try:
        return _parse_records(section.get("user", []), default_source="user")
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


#: 沙箱面板写的记录的来源标记（``migrated_from`` 兼作记录来源；见 models）
ORIGIN_SANDBOX_PANEL = "sandbox_panel"


def replace_records_by_origin(
    *,
    origin: str,
    list_type: str,
    records: list[SecurityListRecord],
    reject_conflicts: bool = False,
) -> dict[str, Any]:
    """把 ``origin`` 名下、``list_type`` 类的记录**整体替换**为 ``records``（一个事务）。

    用途：``sandbox.files.set`` / ``sandbox.network.set`` 这类**只有 set 没有 delete**
    的写面——"从列表里移除一条"只能靠"不在新列表里"推断，所以必须先知道"之前哪些
    是这个写面写的"。``migrated_from`` 兼作记录来源标记（与 ``net_guard`` /
    ``file_guard`` / ``sandbox_copy`` 这些迁移来源同一个字段）。

    同操作对象 ``(type, pattern, match)`` 已被**别家**占用 → 跳过并放进 ``skipped``
    （不夺权、也不静默丢：调用方应把 skipped 回报给用户）。

    返回 ``{"created", "removed", "skipped"}``。
    """
    now = utc_now_iso()
    created = 0
    removed = 0
    skipped: list[str] = []

    def _mutate(data: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal created, removed
        section = _ensure_section(data)
        existing = _parse_existing_user(section)
        user_raw = section["user"]

        kept: list[SecurityListRecord] = []
        kept_raw: list[Any] = []
        for rec, raw in zip(existing, user_raw):
            if rec.type == list_type and rec.migrated_from == origin:
                removed += 1
                continue
            kept.append(rec)
            kept_raw.append(raw)

        occupied = {(r.type, r.pattern, r.match) for r in kept}
        for rec in records:
            key = (rec.type, rec.pattern, rec.match)
            if key in occupied:
                if reject_conflicts:
                    raise DuplicateRecordError(f"操作对象已被其它来源占用: {rec.pattern}")
                skipped.append(rec.pattern)
                logger.warning(
                    "按来源替换跳过（操作对象已被其它来源占用）: origin=%s %s %r",
                    origin, rec.type, rec.pattern,
                )
                continue
            rec.id = rec.id or new_record_id()
            if any(r.id == rec.id for r in kept):
                rec.id = new_record_id()
            rec.enabled = True
            rec.source = "user"
            rec.migrated_from = origin
            rec.created_at = rec.created_at or now
            rec.updated_at = now
            validate_record(rec, existing=kept)
            kept.append(rec)
            kept_raw.append(record_to_dict(rec))
            occupied.add(key)
            created += 1

        section["user"] = kept_raw
        return data

    update_config(_mutate)
    logger.info(
        "按来源整体替换: origin=%s type=%s created=%d removed=%d skipped=%d",
        origin, list_type, created, removed, len(skipped),
    )
    return {"created": created, "removed": removed, "skipped": skipped}


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
        return data if found else None

    update_config(_mutate)
    if found:
        logger.info("security_lists delete: id=%s", record_id)
    return found


def cloud_sync(
    *,
    sync_version: str,
    records: list[dict[str, Any]],
    synced_at: str,
    defaults: dict[str, Any] | None = None,
) -> int:
    """cloud 区整区替换（user 区不动）。

    records 逐条校验，**任一非法整批拒绝**（抛 :class:`ValueError`，RPC 映射 400）；
    批次内 ``(type, pattern, match)`` 重复同样整批拒绝。

    ``defaults`` 非 ``None`` 时替换 cloud.defaults（用户 defaults 不动）；
    缺省时保留前次云侧兜底档。用户完整回退链优先于云侧完整回退链。
    返回应用条数。
    """
    parsed: list[SecurityListRecord] = []
    for raw in records:
        rec = record_from_dict(raw, default_source="cloud")
        rec.source = "cloud"
        validate_record(rec, existing=parsed)
        parsed.append(rec)
    if defaults is not None:
        validate_defaults(defaults)   # 非法整批拒绝，不落盘

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = _ensure_section(data)
        now = utc_now_iso()
        for rec in parsed:
            if not rec.id:
                rec.id = new_record_id()
            rec.created_at = rec.created_at or now
            rec.updated_at = now
        previous_defaults = section["cloud"].get("defaults") or {}
        section["cloud"] = {
            "sync_version": str(sync_version or ""),
            "synced_at": str(synced_at or ""),
            "records": [record_to_dict(r) for r in parsed],
        }
        cloud_defaults = previous_defaults if defaults is None else defaults
        if cloud_defaults:
            section["cloud"]["defaults"] = {
                str(mode): {str(list_type): str(action) for list_type, action in row.items()}
                for mode, row in cloud_defaults.items()
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


def _migration_marker(section: dict[str, Any], key: str = _MIGRATION_SANDBOX_COPY) -> str | None:
    migrations = section.get("migrations")
    if not isinstance(migrations, dict):
        return None
    marker = migrations.get(key)
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
    net = win.get("network") if isinstance(win, dict) else copy.get("network")
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


def migrate_sandbox_copy_once(copy_path: Path | None = None) -> int:
    """Windows/Linux 运行时副本 → user 区聚合记录（幂等，只读副本）。

    两份副本分别使用 sandbox_copy / sandbox_copy_linux 迁移标记。
    与存量 user 记录同 ``(type, pattern, match)`` 的迁移条目跳过（用户显式
    配置优先）。**不清空副本**（副本是沙箱侧活配置，见函数尾注释）。
    返回新建记录条数。
    """
    if copy_path is None:
        windows_path = _default_copy_path()
        return (migrate_sandbox_copy_once(windows_path)
                + migrate_sandbox_copy_once(windows_path.with_name("default-policy.runtime.yaml")))
    copy_path = Path(copy_path)
    migration_key = ("sandbox_copy_linux" if copy_path.name == "default-policy.runtime.yaml"
                     else _MIGRATION_SANDBOX_COPY)
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
        if _migration_marker(section, migration_key):
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
        section.setdefault("migrations", {})[migration_key] = now
        return data

    update_config(_mutate)
    if already_done:
        return 0
    # 副本用户段**不清空**：windows-policy.runtime.yaml 是 sandbox.files.set /
    # sandbox.network.set（server/sandbox_policy_render.py 直接读写）与
    # FileGuard 同步（sandbox.files.sync）的活配置，清空会抹掉沙箱 ACL / egress
    # 规则。本名单运行时已不再投影该副本（见 composer），副本归属沙箱侧。
    logger.info("沙箱副本迁移完成: 新建 %d 条名单记录（副本保留）", migrated)
    return migrated


# ---------------------------------------------------------------------------
# 一次性迁移：legacy 段（net_guard.urls / file_guard.paths）→ user 区
# ---------------------------------------------------------------------------


#: 格子严格度（同对象重复条目合并时取大者）
_CELL_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}


def _merge_same_object(records: list[SecurityListRecord]) -> list[SecurityListRecord]:
    """同操作对象（``type``+``pattern``+``match``）多条 → 合入一条，格子取最严。

    legacy 段是列表，同对象重复条目（手改配置 / 历史脏数据）并不罕见。user 区
    ``(type, pattern, match)`` 唯一，不合并就会在 ``validate_record`` 上撞唯一性、
    整批事务中止——或者更糟：按 id 判定"同一条"而**静默丢弃**后一条的格子。
    取最严是安全名单的默认取向：宁可更紧，不可更松。
    """
    merged: dict[tuple[str, str, str], SecurityListRecord] = {}
    for rec in records:
        key = (rec.type, rec.pattern, rec.match)
        kept = merged.get(key)
        if kept is None:
            merged[key] = rec
            continue
        logger.warning(
            "legacy 段同操作对象重复，合并取最严: %s %r", rec.type, rec.pattern
        )
        for mode, row in rec.cells.items():
            target = kept.cells.setdefault(mode, {})
            for op, action in row.items():
                if _CELL_SEVERITY.get(action, 0) > _CELL_SEVERITY.get(target.get(op, ""), -1):
                    target[op] = action
    return list(merged.values())


def _legacy_candidates(
    permissions: Any,
    sources: tuple[str, ...],
) -> tuple[list[SecurityListRecord], dict[str, int]]:
    """按来源投影出待搬记录（复用只读投影，保证cells/match 语义与展示完全一致）。"""
    from .normalize import project_approvals, project_net_guard

    perms = permissions if isinstance(permissions, Mapping) else {}
    candidates: list[SecurityListRecord] = []
    per_source: dict[str, int] = {}

    if "net_guard" in sources:
        net = _merge_same_object(project_net_guard(perms))
        per_source["net_guard"] = len(net)
        for rec in net:
            rec.migrated_from = "net_guard"
            candidates.append(rec)

    if "file_guard" in sources:
        # 投影会一并产出 approval_overrides（type=command），只要 file_path 那部分
        files = [
            r for r in _merge_same_object(project_approvals(permissions=perms))
            if r.type == "file_path"
        ]
        per_source["file_guard"] = len(files)
        for rec in files:
            rec.migrated_from = "file_guard"
            candidates.append(rec)

    return candidates, per_source


def migrate_legacy_once(
    *,
    sources: tuple[str, ...] = _LEGACY_SOURCES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把 legacy 段（``net_guard.urls`` / ``file_guard.paths``）一次性搬进 user 区。

    **搬的是副本，legacy 段不动**。这样做的原因有二：

    1. **强制点仍读 legacy**：core ``FileGuardChecker`` 读 ``file_guard.paths``、
       ``NetGuardChecker`` 读 ``net_guard.urls``（含宿主 HTTP 出口的 P3 逐跳
       3xx 校验——rail 只看得到工具参数，够不到那一层）。清空 legacy 段等于让
       这些强制点掉规则，是安全回退。
    2. **可回滚**：删掉本轮新建的记录（``migrated_from`` 标记）与 ``migrations``
       标记即可回到迁移前状态。

    搬完后由 :meth:`SecurityListComposer.collect` 的**让位**逻辑生效：同一操作
    对象以物理记录为唯一真源，legacy 投影不再产出同名记录——新安全中心因此
    能真正"删得掉、改得动"。

    一次性（每来源独立 ``migrations.<source>`` 标记，便于灰度逐段放开）：搬迁后
    用户在 legacy 段**新增**的条目仍会被投影并强制（其键未被占用），不存在静默失效。
    ``dry_run`` 只返回候选，不落盘。

    返回 ``{"candidates", "created", "skipped", "sources", "records"}``。
    """
    unknown = [s for s in sources if s not in _LEGACY_SOURCES]
    if unknown:
        raise ValueError(f"未知迁移来源: {unknown!r}（可选 {list(_LEGACY_SOURCES)}）")

    from jiuwenswarm.common.config import get_config

    try:
        cfg = get_config()
    except Exception as exc:  # noqa: BLE001
        raise SecurityListsCorruptedError(f"config.yaml 解析失败，迁移中止: {exc}") from exc
    permissions = cfg.get("permissions") if isinstance(cfg, Mapping) else None

    candidates, per_source = _legacy_candidates(permissions, tuple(sources))
    result: dict[str, Any] = {
        "candidates": len(candidates),
        "created": 0,
        "skipped": 0,
        "sources": per_source,
        "records": candidates,
    }
    if dry_run or not candidates:
        return result

    created = 0
    skipped = 0
    now = utc_now_iso()

    def _mutate(data: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal created, skipped
        section = _ensure_section(data, bootstrap_legacy=False)
        migrations = section.setdefault("migrations", {})
        existing = _parse_existing_user(section)
        user_raw = section["user"]
        occupied = {(r.type, r.pattern, r.match) for r in existing}
        known_ids = {r.id for r in existing if r.id}
        changed = False

        for rec in candidates:
            source = rec.migrated_from or ""
            if migrations.get(source):
                continue          # 该来源已搬过（灰度下逐段放开）
            key = (rec.type, rec.pattern, rec.match)
            if key in occupied:
                logger.info("legacy 迁移跳过（用户已配置同操作对象）: %s %r", rec.type, rec.pattern)
                skipped += 1
                continue
            rec.source = "user"
            rec.created_at = rec.created_at or now
            rec.updated_at = now
            if not rec.id or rec.id in known_ids:
                rec.id = new_record_id()
            validate_record(rec, existing=existing)
            user_raw.append(record_to_dict(rec))
            existing.append(rec)
            occupied.add(key)
            known_ids.add(rec.id)
            created += 1
            changed = True

        for source in sources:
            # 无候选（段缺失/总开关关）不盖章：等用户真正配了规则还能再搬
            if per_source.get(source) and not migrations.get(source):
                migrations[source] = now
                changed = True
        return data if changed else None

    try:
        update_config(_mutate)
    except SecurityListsCorruptedError:
        raise
    result["created"] = created
    result["skipped"] = skipped
    logger.info(
        "legacy 名单迁移完成: created=%d skipped=%d sources=%s（legacy 段保留）",
        created,
        skipped,
        sorted(s for s in sources),
    )
    return result
