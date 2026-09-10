# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

POLL_TABLES: tuple[str, ...] = (
    "channel_config",
    "logging_config",
    "log_masking_rule",
)
_POLL_TABLES = frozenset(POLL_TABLES)


def _normalize_updated_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def row_key(table: str, row: dict[str, Any]) -> str:
    if table == "channel_config":
        return str(row.get("channel_id") or "").strip()
    if table == "log_masking_rule":
        return str(row.get("rule_id") or "").strip()
    # logging_config：单文档表
    return str(row.get("id") or "default").strip()


def row_stamp(row: dict[str, Any]) -> str:
    ts = _normalize_updated_at(row.get("updated_at"))
    return ts.isoformat() if ts is not None else ""


def row_snapshot(table: str, rows: list[dict[str, Any]]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for row in rows:
        key = row_key(table, row)
        if not key:
            continue
        snapshot[key] = row_stamp(row)
    return snapshot


async def list_table_records(table: str) -> list[dict[str, Any]]:
    """拉取 poll 表全行：优先 ``PersistentStore``，否则 ``db_queries`` reader。"""
    if table not in _POLL_TABLES:
        logger.warning("[ConfigPoll] unsupported table: %s", table)
        return []

    from jiuwenswarm.gateway.storage.access import get_persistent_store

    store = get_persistent_store()
    if store is not None:
        await store.ensure_ready()
        rows = await store.list(table)
        return [dict(row) for row in rows or []]

    from jiuwenswarm.server.runtime.enterprise_config import db_queries

    return await db_queries.list_records(table)
