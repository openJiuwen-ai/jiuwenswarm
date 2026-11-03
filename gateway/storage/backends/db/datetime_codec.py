# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""datetime 列读写转换：字符串按 UTC 墙钟；MySQL naive，PG aware UTC。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime

_PG_DIALECTS = frozenset({"postgresql", "postgres", "pg", "gaussdb", "opengauss"})


def _parse_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"unsupported datetime value: {type(value)!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dialect_name(handler: Any) -> str:
    engine = handler.get_engine()
    return engine.dialect.name


def _datetime_column_names(handler: Any, table_name: str) -> set[str]:
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
        if isinstance(column.type, DateTime)
    }


def normalize_datetime_columns_for_write(
    handler: Any, table_name: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    if not data:
        return {} if data is None else data
    names = _datetime_column_names(handler, table_name)
    dialect = _dialect_name(handler)
    convert = (
        pg_datetime_for_write
        if dialect in _PG_DIALECTS
        else mysql_datetime_for_write
    )
    out = dict(data)
    for key, value in data.items():
        if key not in names and not isinstance(value, datetime):
            continue
        if isinstance(value, (list, tuple, set)):
            out[key] = [convert(item) for item in value]
        else:
            out[key] = convert(value)
    return out


def mysql_datetime_for_write(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_utc_datetime(value).replace(tzinfo=None)


def pg_datetime_for_write(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_utc_datetime(value)


def normalize_datetime_columns_for_read(
    handler: Any, table_name: str, record: Any
) -> Any:
    if record is None:
        return None
    names = _datetime_column_names(handler, table_name)
    if not names:
        return record
    dialect = _dialect_name(handler)
    convert = (
        pg_datetime_for_read
        if dialect in _PG_DIALECTS
        else mysql_datetime_for_read
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


def mysql_datetime_for_read(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat(sep=" ")
        return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    return _parse_utc_datetime(value).replace(tzinfo=None).isoformat(sep=" ")


def pg_datetime_for_read(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_utc_datetime(value).replace(tzinfo=None).isoformat(sep=" ")


__all__ = [
    "mysql_datetime_for_read",
    "mysql_datetime_for_write",
    "normalize_datetime_columns_for_read",
    "normalize_datetime_columns_for_write",
    "pg_datetime_for_read",
    "pg_datetime_for_write",
]
