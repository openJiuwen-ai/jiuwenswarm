# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业专属表统一 Repository：dict record ↔ PersistentStore，不判断 edition。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.enterprise.catalog import (
    EnterpriseRecordSpec,
    get_enterprise_record_spec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

_LIST_ALL_CAP = 10_000


class EnterpriseRecordRepository:
    """企业配置 / 模板 / 密钥等表的通用 CRUD。

    record 即业务 dict（与 EE ``DBHandler`` 行形状对齐）；不引入领域 dataclass。
    ``instance_id`` / ``scope_field`` 保留兼容；默认无 scope（每网关独立 DB）。
    """

    def __init__(
        self,
        store: PersistentStore,
        store_name: str,
        *,
        instance_id: str = "",
        spec: EnterpriseRecordSpec | None = None,
    ) -> None:
        self._store = store
        self._store_name = store_name
        # 保留构造参数以兼容旧调用方；scope_field 为 None 时不参与读写。
        self._instance_id = str(instance_id or "").strip()
        self._spec = spec if spec is not None else get_enterprise_record_spec(store_name)

    @property
    def store_name(self) -> str:
        return self._store_name

    @property
    def key_fields(self) -> tuple[str, ...]:
        return self._spec.key_fields

    def for_table(
        self,
        store_name: str,
        *,
        spec: EnterpriseRecordSpec | None = None,
    ) -> EnterpriseRecordRepository:
        """复用同一 ``PersistentStore`` 与实例 scope，访问另一张企业表。"""
        return EnterpriseRecordRepository(
            self._store,
            store_name,
            instance_id=self._instance_id,
            spec=spec,
        )

    def _scope_filters(self) -> dict[str, Any]:
        field = self._spec.scope_field
        if not field or not self._instance_id:
            return {}
        return {field: self._instance_id}

    def identity(self, key_parts: dict[str, Any] | None = None) -> dict[str, Any]:
        """组装 get / update / delete 主键：scope + 业务 key_fields。"""
        identity = dict(self._scope_filters())
        parts = dict(key_parts or {})
        for field in self._spec.key_fields:
            if field not in parts:
                raise ValueError(f"{self._store_name}: missing key field {field!r}")
            identity[field] = parts[field]
        return identity

    def _identity_from_record(self, record: dict[str, Any]) -> dict[str, Any]:
        parts = {field: record[field] for field in self._spec.key_fields if field in record}
        if len(parts) != len(self._spec.key_fields):
            missing = [f for f in self._spec.key_fields if f not in record]
            raise ValueError(
                f"{self._store_name}: record missing key fields {missing}"
            )
        return self.identity(parts)

    def _with_scope(self, record: dict[str, Any]) -> dict[str, Any]:
        row = dict(record)
        field = self._spec.scope_field
        if field and self._instance_id:
            row.setdefault(field, self._instance_id)
        return row

    async def get(
        self,
        key_parts: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        parts = dict(key_parts or {})
        parts.update(kwargs)
        return await self._store.get(self._store_name, self.identity(parts))

    async def list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        merged = dict(self._scope_filters())
        if filters:
            merged.update(filters)
        return await self._store.list(
            self._store_name,
            filters=merged or None,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        return await self._store.create(self._store_name, self._with_scope(record))

    async def update(
        self,
        key_parts: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        # 不允许改 scope / 业务主键
        protected = set(self._spec.key_fields)
        if self._spec.scope_field:
            protected.add(self._spec.scope_field)
        safe = {key: value for key, value in updates.items() if key not in protected}
        return await self._store.update(
            self._store_name,
            self.identity(key_parts),
            safe,
        )

    async def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        """按业务主键有则浅合并更新，无则插入。"""
        row = self._with_scope(record)
        key = self._identity_from_record(row)
        existing = await self._store.get(self._store_name, key)
        if existing is None:
            return await self._store.create(self._store_name, row)
        updates = {
            key_: value
            for key_, value in row.items()
            if key_ not in key and key_ != "id"
        }
        updated = await self._store.update(self._store_name, key, updates)
        return updated if updated is not None else row

    async def delete(
        self,
        key_parts: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> bool:
        parts = dict(key_parts or {})
        parts.update(kwargs)
        return await self._store.delete(self._store_name, self.identity(parts))

    async def get_by_row_id(self, row_id: int) -> dict[str, Any] | None:
        """按自增 ``id`` 取行（若有 scope_field 则校验，默认无）。"""
        row = await self._store.get(self._store_name, {"id": int(row_id)})
        if row is None:
            return None
        scope = self._scope_filters()
        for key, value in scope.items():
            if row.get(key) != value:
                return None
        return row

    async def update_by_row_id(
        self,
        row_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        if await self.get_by_row_id(row_id) is None:
            return None
        return await self._store.update(
            self._store_name,
            {"id": int(row_id)},
            dict(updates),
        )

    async def delete_by_row_id(self, row_id: int) -> bool:
        if await self.get_by_row_id(row_id) is None:
            return False
        return await self._store.delete(self._store_name, {"id": int(row_id)})

    async def sync_by_business_key(
        self,
        records: list[dict[str, Any]],
        *,
        key_field: str | None = None,
    ) -> dict[str, int]:
        """按业务主键 upsert，并删除不在 incoming 集合中的行。

        默认 ``key_field`` 取 ``spec.key_fields[0]``（如 ``policy_id`` / ``template_id``）。
        无业务主键（单文档表）时只支持 0～1 条：有则 upsert，空列表则 delete。
        """
        fields = self._spec.key_fields
        if not fields:
            if not records:
                existing = await self.list(limit=1)
                deleted = 0
                if existing:
                    if await self.delete():
                        deleted = 1
                return {"synced_count": 0, "deleted_count": deleted}
            if len(records) > 1:
                raise ValueError(
                    f"{self._store_name}.sync: single-document store accepts at most one record"
                )
            await self.upsert(records[0])
            return {"synced_count": 1, "deleted_count": 0}

        field = key_field or fields[0]
        if field not in fields:
            raise ValueError(
                f"{self._store_name}.sync: key_field {field!r} not in {fields}"
            )

        incoming: set[str] = set()
        synced = 0
        for item in records:
            if not isinstance(item, dict):
                raise ValueError(f"{self._store_name}.sync records must be objects")
            raw = str(item.get(field) or "").strip()
            if not raw:
                raise ValueError(f"{self._store_name}.sync record missing {field}")
            incoming.add(raw)
            row = dict(item)
            row[field] = raw
            await self.upsert(row)
            synced += 1

        deleted = 0
        existing_rows = await self.list(limit=_LIST_ALL_CAP)
        for row in existing_rows:
            value = str(row.get(field) or "")
            if value and value not in incoming:
                if await self.delete({field: value}):
                    deleted += 1
        return {"synced_count": synced, "deleted_count": deleted}


__all__ = ["EnterpriseRecordRepository"]
