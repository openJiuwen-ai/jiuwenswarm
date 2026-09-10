# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Gateway 本地库 facade（企业版）。

经 ``Database.current().ensure_ready`` 直连取 foundation ``DBHandler``，
CRUD 调 ``list_records / create / update``。
供 AgentServer 等无 PersistentStore 装配的进程使用；每网关独立数据库，
查询不加实例行级隔离。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from jiuwenswarm.common.utils import logger

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# foundation ``list_records`` 默认 limit=100；此处分页拉取全表行。
# _PAGE_SIZE：单页行数；_TOTAL_LIMIT：安全上限，防止异常大表撑爆内存。
# 触达 _TOTAL_LIMIT 时记录 warning 并停止，不再静默截断。
_PAGE_SIZE = 1_000
_TOTAL_LIMIT = 1_000_000


# --------------------------------------------------------------------------- #
# 标识符 / 行转换辅助
# --------------------------------------------------------------------------- #
def is_safe_ident(name: str) -> bool:
    return bool(_SAFE_IDENT.fullmatch(name or ""))


def _parse_json_string(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def row_to_dict(row: Any) -> dict[str, Any]:
    """DB 行 → dict；字符串 JSON 字段自动 parse。

    兼容 dict / SQLAlchemy ``Row``（``_mapping``）/ foundation ORM 实例
    （``__table__`` + ``vars``，过滤 ``_sa_`` 内部态）。
    """
    if isinstance(row, dict):
        items = dict(row)
    else:
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            items = dict(mapping)
        else:
            keys_fn = getattr(type(row), "keys", None)
            if callable(keys_fn):
                items = {k: row[k] for k in keys_fn(row)}
            elif hasattr(row, "__table__"):
                items = {k: v for k, v in vars(row).items() if not k.startswith("_sa_")}
            elif hasattr(row, "__dict__"):
                # SimpleNamespace（manager_config_receiver 适配层返回的行）等普通对象
                items = {k: v for k, v in vars(row).items() if not k.startswith("_sa_")}
            else:
                items = dict(row)
    out: dict[str, Any] = {}
    for key, value in items.items():
        out[key] = _parse_json_string(value) if isinstance(value, str) else value
    return out


def sort_by_order(rows: list[dict[str, Any]], order_by: str) -> list[dict[str, Any]]:
    """内存排序兜底，支持 ``-field`` / ``field DESC`` 语法。"""
    text = order_by.strip()
    if not text:
        return rows

    parts = text.split(None, 1)
    field = parts[0].strip()
    reverse = False
    if len(parts) > 1:
        reverse = parts[1].strip().upper() == "DESC"
    elif field.startswith("-"):
        reverse = True
        field = field[1:].strip()
    if not field:
        return rows

    def _key(row: dict[str, Any]) -> Any:
        value = row.get(field)
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    return sorted(rows, key=_key, reverse=reverse)


# --------------------------------------------------------------------------- #
# 存储屏蔽层入口
# --------------------------------------------------------------------------- #
async def _handler() -> Any:
    """直连 ``Database`` 单例。"""
    from jiuwenswarm.infrastructure.db.database import Database

    return await Database.current().ensure_ready(log_prefix="gateway_db_reader")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
async def list_records(
    table: str,
    query: dict[str, Any] | None = None,
    order_by: str = "",
) -> list[dict[str, Any]]:
    """等值 filters 列表查询（每网关独立 DB，不加实例隔离列）。

    分页拉取全表行：以 ``_PAGE_SIZE`` 步进 ``offset``，返回不足一页即结束。
    累计行数触达 ``_TOTAL_LIMIT`` 安全上限时记录 warning 并停止，避免静默
    截断。注意：offset 分页在并发写入下可能短暂跳/重行，调用方
    ``config_poll`` 以 dict 去重 + 周期轮询自愈，可接受。
    """
    if not is_safe_ident(table or ""):
        logger.warning("[gateway_db_reader] invalid table name: %r", table)
        return []

    handler = await _handler()
    filters = dict(query or {})
    sort = order_by or None
    try:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await handler.list_records(
                table,
                filters=filters,
                limit=_PAGE_SIZE,
                offset=offset,
                order_by=sort,
            )
            if not page:
                break
            out.extend(row_to_dict(r) for r in page)
            if len(page) < _PAGE_SIZE:
                break
            offset += len(page)
            if len(out) >= _TOTAL_LIMIT:
                logger.warning(
                    "[gateway_db_reader] list %s hit total limit %d (truncated); "
                    "narrow filters or raise _TOTAL_LIMIT",
                    table,
                    _TOTAL_LIMIT,
                )
                break
        return out
    except Exception as exc:  # noqa: BLE001
        logger.error("[gateway_db_reader] list %s failed: %s", table, exc, exc_info=exc)
        raise


__all__ = [
    "is_safe_ident",
    "list_records",
    "row_to_dict",
    "sort_by_order",
]
