# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""单测用内存 PersistentStore。"""

from __future__ import annotations

from typing import Any


def _matches(row: dict[str, Any], key: dict[str, Any]) -> bool:
    return all(row.get(k) == v for k, v in key.items())


def _apply_order(
    rows: list[dict[str, Any]],
    order_by: str,
) -> list[dict[str, Any]]:
    if not order_by:
        return rows
    reverse = " DESC" in order_by.upper()
    field = order_by.replace(" DESC", "").replace(" desc", "")
    field = field.replace(" ASC", "").replace(" asc", "").strip()
    if not field:
        return rows
    rows.sort(
        key=lambda item: (item.get(field) is None, item.get(field)),
        reverse=reverse,
    )
    return rows


class InMemoryPersistentBackend:
    """dict 模拟的 PersistentStore，不读布局注册表。"""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}

    async def ensure_ready(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._tables.get(name, [])]
        key = dict(filters or {})
        if key:
            rows = [row for row in rows if _matches(row, key)]
        rows = _apply_order(rows, order_by)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self.list(name, filters=key, limit=1)
        return rows[0] if rows else None

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        tables = self._tables.setdefault(name, [])
        created = dict(record)
        if "id" not in created:
            created["id"] = len(tables) + 1
        tables.append(created)
        return dict(created)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按主键浅合并 ``updates``；找不到记录返回 None。不删字段。"""
        rows = self._tables.get(name, [])
        for idx, row in enumerate(rows):
            if _matches(row, key):
                updated = dict(row)
                updated.update(updates)
                rows[idx] = updated
                return dict(updated)
        return None

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        rows = self._tables.get(name, [])
        for idx, row in enumerate(rows):
            if _matches(row, key):
                rows.pop(idx)
                return True
        return False


__all__ = ["InMemoryPersistentBackend"]
