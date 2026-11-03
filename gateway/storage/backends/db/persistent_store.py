# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DB PersistentStore：name → 表，不注入业务列。

``POLL_REVISION_TABLES`` 例外：插入缺省 ``revision = 1``。更新必须带上读到的
``revision``（整数或 ``ExpectedRevision``），语句为
``WHERE revision = 读到的值`` 且 ``SET revision = revision + 1``。
对不上则抛 ``RevisionConflictError``，不覆盖已提交的写入。
"""

from __future__ import annotations

from typing import Any

from gateway.config.enterprise.tables.application_config_models import (
    POLL_REVISION_TABLES,
)
from gateway.storage.backends.db.connection import PersistentDbConnection
from gateway.storage.backends.db.bool_codec import (
    normalize_boolean_columns_for_read,
    normalize_boolean_columns_for_write,
)
from gateway.storage.backends.db.datetime_codec import (
    normalize_datetime_columns_for_read,
    normalize_datetime_columns_for_write,
)
from gateway.storage.backends.db.records import row_to_dict
from gateway.storage.errors import StorageUnavailableError
from gateway.storage.registry.store_registry import StoreRegistry
from gateway.storage.revision import RevisionConflictError, revision_of


class RevisionIncrement:
    """UPDATE 标记：数据库执行 ``column = column + 1``。"""

    sql_column_increment = True


def _stamp_revision_on_create(table: str, record: dict[str, Any]) -> dict[str, Any]:
    if table not in POLL_REVISION_TABLES or record.get("revision") is not None:
        return record
    stamped = dict(record)
    stamped["revision"] = 1
    return stamped


def _optimistic_update(
    table: str,
    key: dict[str, Any],
    updates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """轮询表：``revision`` 是读到的版本，放进 WHERE，SET 由数据库加一。"""
    if table not in POLL_REVISION_TABLES:
        return key, updates
    expected = revision_of(updates)
    if expected is None:
        raise ValueError(
            f"{table} update requires the revision read with this change"
        )
    match_key = dict(key)
    match_key["revision"] = expected
    payload = {key_: value for key_, value in updates.items() if key_ != "revision"}
    payload["revision"] = RevisionIncrement()
    return match_key, payload


class DbPersistentBackend:
    """通用 dict CRUD；表名来自注入的布局注册表。"""

    def __init__(
        self,
        connection: PersistentDbConnection,
        registry: StoreRegistry,
    ) -> None:
        self._connection = connection
        self._registry = registry

    def _table(self, name: str) -> str:
        layout = self._registry.get(name)
        if layout is None or layout.db is None:
            raise StorageUnavailableError(f"name {name!r} has no db layout")
        return layout.db.table

    async def ensure_ready(self) -> None:
        await self._connection.ensure_ready()

    async def close(self) -> None:
        await self._connection.close()

    @staticmethod
    def _write(handler: Any, table: str, data: dict[str, Any]) -> dict[str, Any]:
        data = normalize_datetime_columns_for_write(handler, table, data)
        return normalize_boolean_columns_for_write(handler, table, data)

    @staticmethod
    def _read(handler: Any, table: str, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        data = normalize_datetime_columns_for_read(handler, table, row_to_dict(row))
        return normalize_boolean_columns_for_read(handler, table, data)

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        rows = await handler.list_records(
            table,
            self._write(handler, table, dict(filters or {})),
            limit=10_000 if limit is None else limit,
            offset=offset,
            order_by=order_by,
        )
        return [self._read(handler, table, row) for row in rows or []]

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        row = await handler.get(table, self._write(handler, table, dict(key)))
        return self._read(handler, table, row)

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        payload = _stamp_revision_on_create(
            table, self._write(handler, table, dict(record))
        )
        created = await handler.create(table, payload)
        return self._read(handler, table, created)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按主键浅合并 ``updates``；找不到记录返回 None。不删字段。

        轮询表在 ``WHERE`` 中带上读到的 ``revision``。版本不符时抛
        ``RevisionConflictError``，已提交的那次写入保持不变。
        还没有读到 revision 时：行不存在返回 None，行已存在则抛冲突，
        调用方重读后再写。
        """
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        encoded_key = self._write(handler, table, dict(key))
        encoded_updates = self._write(handler, table, dict(updates))
        if table in POLL_REVISION_TABLES and revision_of(encoded_updates) is None:
            # 还没读到行（例如第一次写入频道）。没有 revision 不能更新；
            # 行不存在时交给调用方插入。行已经存在则让调用方重读再写。
            current = await handler.get(table, encoded_key)
            if current is None:
                return None
            raise RevisionConflictError(
                f"{table} update requires the revision read with this change"
            )
        match_key, payload = _optimistic_update(table, encoded_key, encoded_updates)
        updated = await handler.update(table, match_key, payload)
        if updated is not None:
            return self._read(handler, table, updated)
        if table not in POLL_REVISION_TABLES:
            return None
        current = await handler.get(table, encoded_key)
        if current is None:
            return None
        raise RevisionConflictError(
            f"{table} revision conflict: expected {match_key.get('revision')}"
        )

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        return bool(
            await handler.delete(table, self._write(handler, table, dict(key)))
        )


__all__ = ["DbPersistentBackend"]
