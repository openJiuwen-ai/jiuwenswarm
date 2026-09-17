# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""boolean 列读写转换：业务可传 0/1；MySQL 绑 0/1，PG 绑 True/False。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean

_PG_DIALECTS = frozenset({"postgresql", "postgres", "pg", "gaussdb", "opengauss"})


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in {"1", "true", "t", "yes"}:
            return True
        if text in {"0", "false", "f", "no"}:
            return False
    return bool(value)


def _dialect_name(handler: Any) -> str:
    engine = handler.get_engine()
    return engine.dialect.name


def _boolean_column_names(handler: Any, table_name: str) -> set[str]:
    get_table = getattr(handler, "get_table", None)
    if not callable(get_table):
        return set()
    try:
        table = get_table(table_name)
    except (ValueError, AttributeError):
        return set()
    return {
        column.name
        for column in table.columns
        if isinstance(column.type, Boolean)
    }


def normalize_boolean_columns_for_write(
    handler: Any, table_name: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    if not data:
        return {} if data is None else data
    names = _boolean_column_names(handler, table_name)
    dialect = _dialect_name(handler)
    convert = (
        pg_boolean_for_write
        if dialect in _PG_DIALECTS
        else mysql_boolean_for_write
    )
    out = dict(data)
    for key, value in data.items():
        if key not in names and not isinstance(value, bool):
            continue
        if isinstance(value, (list, tuple, set)):
            out[key] = [convert(item) for item in value]
        else:
            out[key] = convert(value)
    return out


def mysql_boolean_for_write(value: Any) -> Any:
    parsed = _as_bool(value)
    if parsed is None:
        return None
    return 1 if parsed else 0


def pg_boolean_for_write(value: Any) -> Any:
    return _as_bool(value)


def normalize_boolean_columns_for_read(
    handler: Any, table_name: str, record: Any
) -> Any:
    if record is None:
        return None
    names = _boolean_column_names(handler, table_name)
    if not names:
        return record
    dialect = _dialect_name(handler)
    convert = (
        pg_boolean_for_read
        if dialect in _PG_DIALECTS
        else mysql_boolean_for_read
    )
    if isinstance(record, dict):
        out = dict(record)
        for name in names:
            if name in out:
                out[name] = convert(out[name])
        return out
    for name in names:
        setattr(record, name, convert(getattr(record, name, None)))
    return record


def mysql_boolean_for_read(value: Any) -> Any:
    parsed = _as_bool(value)
    if parsed is None:
        return None
    return 1 if parsed else 0


def pg_boolean_for_read(value: Any) -> Any:
    return _as_bool(value)


__all__ = [
    "mysql_boolean_for_read",
    "mysql_boolean_for_write",
    "normalize_boolean_columns_for_read",
    "normalize_boolean_columns_for_write",
    "pg_boolean_for_read",
    "pg_boolean_for_write",
]
