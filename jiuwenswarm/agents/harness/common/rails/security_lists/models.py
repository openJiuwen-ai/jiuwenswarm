# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一安全名单数据模型（v3 聚合记录）。

一个操作对象 ``(type, pattern, match)`` 一条记录，内含 ``cells`` 稀疏矩阵::

    cells[模式|"*"][操作|"*"] = "allow" | "ask" | "deny"

``"*"`` 为**通用格**：未单独设置的模式的默认取值，模式特化格优先。
格子回退链见 :func:`resolve_cell`。

键空间：
- 模式键：``default`` / ``auto_approve`` / ``full_access`` / ``"*"``
- 操作键：``file_path`` → ``read`` / ``write`` / ``exec`` / ``"*"``；
  ``domain`` / ``command`` / ``tool`` → 仅 ``"*"``

四类名单类型：``file_path``（路径）/ ``domain``（域名）/ ``command``（命令）/
``tool``（工具名，见功能设计 §10.3）。工具名是有限枚举字面量，故 ``match``
只有 ``exact``，操作轴只有"是否允许调用"这一个通用格。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

ModeKey = Literal["default", "auto_approve", "full_access", "*"]
OpKey = Literal["read", "write", "exec", "*"]
Action = Literal["allow", "ask", "deny"]
ListType = Literal["file_path", "domain", "command", "tool"]
MatchKind = Literal["glob", "prefix", "exact", "wildcard", "regex"]

#: 合法模式键（含通用格 "*"）
MODE_KEYS: tuple[str, ...] = ("default", "auto_approve", "full_access", "*")
#: file_path 合法操作键
FILE_PATH_OPS: tuple[str, ...] = ("read", "write", "exec", "*")
#: domain / command / tool 合法操作键（仅通用格）
GENERIC_OPS: tuple[str, ...] = ("*",)
#: 合法格子值
ACTIONS: tuple[str, ...] = ("allow", "ask", "deny")

#: 段结构版本（v3 起含 ``defaults`` 兜底档；段内缺 ``version`` 视为 v2 兼容读）
SECURITY_LISTS_VERSION: int = 3

#: ``defaults`` 的「类型键」空间（含通用格 ``"*"``＝所有名单类型）
DEFAULT_TYPE_KEYS: tuple[str, ...] = ("file_path", "domain", "command", "tool", "*")

#: type → 合法 match 组合（设计文档 4.1 / 10.3）
ALLOWED_MATCH: dict[str, tuple[str, ...]] = {
    "file_path": ("glob", "prefix"),
    "domain": ("exact", "wildcard"),
    "command": ("exact", "glob", "regex"),
    # 工具名是有限枚举字面量：通配/正则会带来"哪些工具被管"不可枚举的审计盲区
    "tool": ("exact",),
}

_GLOB_CHARS_RE = re.compile(r"[*?\[]")


class SecurityListsCorruptedError(Exception):
    """security_lists 段解析/校验失败（rail 据此 fail-closed，RPC 映射 500）。"""


class DuplicateRecordError(ValueError):
    """``(type, pattern, match)`` 在目标区内重复（RPC 映射 409）。"""


def new_record_id() -> str:
    """生成记录 id（``ul_<uuid8>``）。"""
    return f"ul_{uuid.uuid4().hex[:8]}"


def utc_now_iso() -> str:
    """ISO8601 UTC 时间戳（秒级，保持 config.yaml 整洁）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SecurityListRecord:
    """聚合名单记录：一个操作对象 + cells 稀疏矩阵。

    ``source``：``user``（安全中心配置）/ ``cloud``（云侧下发）为物理记录；
    ``builtin`` / ``user_approval`` 仅出现在 normalize 投影中，不落盘。

    ``origin``：**投影期注记，不落盘**，用来区分同一 ``source`` 下的不同来路。
    典型场景：``user_approval`` 既可能是审批流"永久记住"生成的，也可能是
    ``file_guard.paths`` 的 legacy 段兼容读——只看 ``source`` 会把后者错误地
    标成"审批记住"（来源标签是错的）。取值见各 ``project_*``：
    ``approval`` / ``file_guard`` / ``net_guard``；``builtin`` 与物理记录的
    ``source`` 已自证来源，留空。（``sandbox_copy`` 迁移记录另看 ``migrated_from``。）
    """

    id: str = ""
    type: str = "file_path"
    pattern: str = ""
    match: str = "glob"
    enabled: bool = True
    note: str = ""
    cells: dict[str, dict[str, str]] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    source: str = "user"
    migrated_from: str | None = None
    #: 投影期来路注记（不落盘，见类 docstring）
    origin: str = ""


def ops_for_type(list_type: str) -> tuple[str, ...]:
    """该名单类型允许的操作键集合。"""
    return FILE_PATH_OPS if list_type == "file_path" else GENERIC_OPS


def has_glob_chars(pattern: str) -> bool:
    """pattern 是否含 glob 元字符（迁移时判定 match 用）。"""
    return bool(_GLOB_CHARS_RE.search(pattern))


def validate_pattern(list_type: str, match: str, pattern: str) -> None:
    """pattern 基础校验（设计文档 4.1）。

    - 非空；
    - domain：无 scheme/路径/端口/查询；wildcard 须 ``*.`` 开头；exact 不含 ``*``；
    - command + regex：可编译。
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern 不能为空")
    if list_type == "domain":
        s = pattern.strip()
        if "://" in s or any(c in s for c in "/:?#"):
            raise ValueError(f"domain pattern 不应含 scheme/路径/端口/查询: {pattern!r}")
        if match == "wildcard" and not s.startswith("*."):
            raise ValueError(f"wildcard domain 须以 '*.' 开头: {pattern!r}")
        if match == "exact" and "*" in s:
            raise ValueError(f"exact domain 不应含通配符: {pattern!r}")
    if list_type == "command" and match == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"command regex 不可编译: {exc}") from exc


def validate_cells(list_type: str, cells: dict[str, dict[str, str]]) -> None:
    """cells 键空间与取值校验（结构须为 dict[mode, dict[op, action]]）。"""
    if not isinstance(cells, dict):
        raise ValueError(f"cells 须为映射: {type(cells).__name__}")
    allowed_ops = ops_for_type(list_type)
    for mode, row in cells.items():
        if mode not in MODE_KEYS:
            raise ValueError(f"未知模式键: {mode!r}")
        if not isinstance(row, dict):
            raise ValueError(f"cells[{mode!r}] 须为映射: {type(row).__name__}")
        for op, action in row.items():
            if op not in allowed_ops:
                raise ValueError(f"{list_type} 不支持操作键: {op!r}")
            if action not in ACTIONS:
                raise ValueError(f"未知格子值: {action!r}")


def validate_record(
    rec: SecurityListRecord,
    *,
    existing: list[SecurityListRecord] | tuple[SecurityListRecord, ...] = (),
) -> None:
    """校验单条记录（设计文档 4.1）。

    - type/match 组合合法；
    - cells 键空间与取值合法；空 cells 合法（无表态记录）但告警；
    - pattern 基础校验；
    - 唯一性：``(type, pattern, match)`` 在 ``existing`` 内不重复
      （按 id 跳过自身；pattern 按原始字符串比较，大小写/斜杠归一化是
      matcher 的职责，不在此做）→ :class:`DuplicateRecordError`。
    """
    if rec.type not in ALLOWED_MATCH:
        raise ValueError(f"未知名单类型: {rec.type!r}")
    if rec.match not in ALLOWED_MATCH[rec.type]:
        raise ValueError(f"{rec.type} 不支持 match={rec.match!r}")
    if not isinstance(rec.enabled, bool):
        raise ValueError(f"enabled 须为布尔: {rec.enabled!r}")
    validate_pattern(rec.type, rec.match, rec.pattern)
    validate_cells(rec.type, rec.cells)
    if not rec.cells:
        logger.warning("名单记录 %s (%s) cells 为空（无表态记录）", rec.id or "<new>", rec.pattern)
    for old in existing:
        if old.id and rec.id and old.id == rec.id:
            continue
        if (old.type, old.pattern, old.match) == (rec.type, rec.pattern, rec.match):
            raise DuplicateRecordError(
                f"操作对象已存在: type={rec.type} pattern={rec.pattern!r} match={rec.match}"
            )


def resolve_cell(cells: dict[str, dict[str, str]], mode: str, op: str) -> str | None:
    """格子回退链解析。

    ``cells[mode][op] → cells[mode]["*"] → cells["*"][op] → cells["*"]["*"]``，
    最具体者胜；全空返回 ``None``（无表态）。读取路径宽容（未校验的畸形行跳过），
    严格性由 :func:`validate_cells` 保证。
    """
    if not cells:
        return None
    mode_row = cells.get(mode)
    if isinstance(mode_row, dict):
        action = mode_row.get(op)
        if action is not None:
            return action
        action = mode_row.get("*")
        if action is not None:
            return action
    star_row = cells.get("*")
    if isinstance(star_row, dict):
        action = star_row.get(op)
        if action is not None:
            return action
        return star_row.get("*")
    return None


def validate_defaults(defaults: Any) -> None:
    """兜底档 ``defaults`` 键空间校验（结构须为 ``dict[模式, dict[类型, 动作]]``）。

    与 :func:`validate_cells` 同构，只是「操作键」换成名单类型键：
    ``defaults[模式|"*"][类型|"*"]``；空映射合法（无兜底＝保持现状）。
    """
    if not isinstance(defaults, dict):
        raise ValueError(f"defaults 须为映射: {type(defaults).__name__}")
    for mode, row in defaults.items():
        if mode not in MODE_KEYS:
            raise ValueError(f"未知模式键: {mode!r}")
        if not isinstance(row, dict):
            raise ValueError(f"defaults[{mode!r}] 须为映射: {type(row).__name__}")
        for list_type, action in row.items():
            if list_type not in DEFAULT_TYPE_KEYS:
                raise ValueError(f"defaults 不支持类型键: {list_type!r}")
            if action not in ACTIONS:
                raise ValueError(f"未知兜底动作: {action!r}")


def resolve_default(defaults: Any, mode: str, list_type: str) -> str | None:
    """兜底档回退链解析（仅在所有记录都不匹配时调用）。

    ``defaults[mode][type] → defaults[mode]["*"] → defaults["*"][type] → defaults["*"]["*"]``，
    最具体者胜；全空/无 ``defaults`` 段返回 ``None``＝**无表态**（交权限管线，存量语义）。
    与 :func:`resolve_cell` 同一条链——此处「操作键」即名单类型键。
    """
    if not isinstance(defaults, dict):
        return None
    return resolve_cell(defaults, mode, list_type)


def record_to_dict(rec: SecurityListRecord) -> dict[str, Any]:
    """序列化为可落盘 dict（空 note / migrated_from 省略，保持 YAML 整洁）。

    ``origin`` **不落盘**——它是投影期注记（物理记录的归属由 ``source`` 决定）。
    """
    data: dict[str, Any] = {
        "id": rec.id,
        "type": rec.type,
        "pattern": rec.pattern,
        "match": rec.match,
        "enabled": rec.enabled,
        "cells": {mode: dict(row) for mode, row in rec.cells.items()},
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "source": rec.source,
    }
    if rec.note:
        data["note"] = rec.note
    if rec.migrated_from:
        data["migrated_from"] = rec.migrated_from
    return data


def record_from_dict(data: Any, *, default_source: str = "user") -> SecurityListRecord:
    """从 dict 反序列化（严格：结构畸形抛 :class:`ValueError`）。

    未知顶层键忽略（前向兼容：云侧/新版可能携带本版不认识的字段）。
    语义校验（type/match 组合、cells 键空间、唯一性）由 :func:`validate_record`
    负责，调用方按需执行。
    """
    if not isinstance(data, dict):
        raise ValueError(f"记录须为映射: {type(data).__name__}")
    for key in ("type", "pattern", "match"):
        if not isinstance(data.get(key), str) or not data.get(key):
            raise ValueError(f"记录缺必填字段或类型错误: {key}")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled 须为布尔: {enabled!r}")
    cells_raw = data.get("cells") or {}
    if not isinstance(cells_raw, dict):
        raise ValueError(f"cells 须为映射: {type(cells_raw).__name__}")
    cells: dict[str, dict[str, str]] = {}
    for mode, row in cells_raw.items():
        if not isinstance(row, dict):
            raise ValueError(f"cells[{mode!r}] 须为映射: {type(row).__name__}")
        cells[str(mode)] = {str(op): str(action) for op, action in row.items()}
    note = data.get("note") or ""
    migrated_from = data.get("migrated_from")
    return SecurityListRecord(
        id=str(data.get("id") or ""),
        type=str(data["type"]),
        pattern=str(data["pattern"]),
        match=str(data["match"]),
        enabled=enabled,
        note=str(note),
        cells=cells,
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        source=str(data.get("source") or default_source),
        migrated_from=str(migrated_from) if migrated_from else None,
    )
