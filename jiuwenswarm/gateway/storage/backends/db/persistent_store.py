# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DB PersistentStore：name → 表，不注入业务列。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.storage.backends.db.connection import PersistentDbConnection
from jiuwenswarm.gateway.storage.backends.db.bool_codec import (
    normalize_boolean_columns_for_read,
    normalize_boolean_columns_for_write,
)
from jiuwenswarm.gateway.storage.backends.db.datetime_codec import (
    normalize_datetime_columns_for_read,
    normalize_datetime_columns_for_write,
)
from jiuwenswarm.gateway.storage.backends.db.records import row_to_dict
from jiuwenswarm.gateway.storage.errors import StorageUnavailableError
from jiuwenswarm.gateway.storage.registry.store_registry import StoreRegistry


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
        created = await handler.create(table, self._write(handler, table, dict(record)))
        return self._read(handler, table, created)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按主键浅合并 ``updates``；找不到记录返回 None。不删字段。"""
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        updated = await handler.update(
            table,
            self._write(handler, table, dict(key)),
            self._write(handler, table, dict(updates)),
        )
        return self._read(handler, table, updated)

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        handler = await self._connection.ensure_ready()
        table = self._table(name)
        return bool(
            await handler.delete(table, self._write(handler, table, dict(key)))
        )


__all__ = ["DbPersistentBackend"]
