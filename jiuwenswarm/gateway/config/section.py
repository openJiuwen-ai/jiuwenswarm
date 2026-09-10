# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""单文档 overlay 共用：一份 YAML 段 / 一行企业表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore
from jiuwenswarm.infrastructure.utils import utc_now


@dataclass
class SectionDocument:
    """整段配置。``body`` 即业务使用的 dict。"""

    body: dict[str, Any] = field(default_factory=dict)
    source: str = "local"
    revision: int = 1


class SectionDocumentCodec(Protocol):
    def identity(self, instance_id: str = "") -> dict[str, Any]:
        """get / update / delete 主键；YAML 单文档通常为 ``{}``。"""

    def from_record(self, record: dict[str, Any]) -> SectionDocument:
        ...

    def to_record(
        self, document: SectionDocument, *, instance_id: str = ""
    ) -> dict[str, Any]:
        ...

    def to_updates(self, document: SectionDocument) -> dict[str, Any]:
        ...


class YamlSectionCodec:
    """personal：YAML 段整份即 record（无主键字段）。"""

    @staticmethod
    def identity(instance_id: str = "") -> dict[str, Any]:
        return {}

    @staticmethod
    def from_record(record: dict[str, Any]) -> SectionDocument:
        body = {
            key: value
            for key, value in dict(record).items()
            if key not in ("id", "source", "revision")
        }
        return SectionDocument(body=body)

    @staticmethod
    def to_record(
        document: SectionDocument, *, instance_id: str = ""
    ) -> dict[str, Any]:
        return dict(document.body)

    @staticmethod
    def to_updates(document: SectionDocument) -> dict[str, Any]:
        return dict(document.body)


class DbBodySectionCodec:
    """enterprise：``body`` JSON 列 + ``source`` / ``revision``（permissions / memory）。"""

    @staticmethod
    def identity(instance_id: str = "") -> dict[str, Any]:
        _ = instance_id
        return {}

    @staticmethod
    def from_record(record: dict[str, Any]) -> SectionDocument:
        raw = record.get("body")
        body = dict(raw) if isinstance(raw, dict) else {}
        return SectionDocument(
            body=body,
            source=str(record.get("source") or "local"),
            revision=int(record.get("revision") or 1),
        )

    @staticmethod
    def to_record(
        document: SectionDocument, *, instance_id: str = ""
    ) -> dict[str, Any]:
        _ = instance_id
        now = utc_now()
        return {
            "body": dict(document.body),
            "source": document.source or "local",
            "revision": int(document.revision or 1),
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def to_updates(document: SectionDocument) -> dict[str, Any]:
        return {
            "body": dict(document.body),
            "source": document.source or "local",
            "revision": int(document.revision or 1),
            "updated_at": utc_now(),
        }


class DbFlatSectionCodec:
    """enterprise：字段即列（logging_config）。"""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self._fields = fields

    @staticmethod
    def identity(instance_id: str = "") -> dict[str, Any]:
        _ = instance_id
        return {}

    def from_record(self, record: dict[str, Any]) -> SectionDocument:
        body = {
            key: record.get(key)
            for key in self._fields
            if key in record and record.get(key) is not None
        }
        return SectionDocument(body=body)

    def to_record(
        self, document: SectionDocument, *, instance_id: str = ""
    ) -> dict[str, Any]:
        _ = instance_id
        now = utc_now()
        row: dict[str, Any] = {
            key: document.body.get(key) for key in self._fields if key in document.body
        }
        if "level" not in row:
            row["level"] = document.body.get("level") or "INFO"
        row["created_at"] = now
        row["updated_at"] = now
        return row

    def to_updates(self, document: SectionDocument) -> dict[str, Any]:
        updates = {
            key: document.body.get(key)
            for key in self._fields
            if key in document.body
        }
        updates["updated_at"] = utc_now()
        return updates


class SectionDocumentRepository:
    """单文档 name 的领域读写；不判断 edition。"""

    def __init__(
        self,
        store: PersistentStore,
        codec: SectionDocumentCodec,
        store_name: str,
        *,
        instance_id: str = "",
    ) -> None:
        self._store = store
        self._codec = codec
        self._store_name = store_name
        self._instance_id = str(instance_id or "").strip()

    def _identity(self) -> dict[str, Any]:
        return self._codec.identity(self._instance_id)

    async def _load_row(self) -> dict[str, Any] | None:
        identity = self._identity()
        if identity:
            return await self._store.get(self._store_name, identity)
        rows = await self._store.list(self._store_name, limit=1)
        return rows[0] if rows else None

    async def get(self) -> SectionDocument | None:
        row = await self._load_row()
        if row is None:
            return None
        return self._codec.from_record(row)

    async def get_body(self) -> dict[str, Any]:
        document = await self.get()
        return dict(document.body) if document else {}

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        """整段替换；YAML 浅 update 不删键，故有旧文档时先 delete 再 create。"""
        existing = await self.get()
        document = SectionDocument(
            body=dict(body),
            source=(existing.source if existing else "local"),
            revision=(int(existing.revision) + 1 if existing else 1),
        )
        if await self._load_row() is not None:
            await self.delete()
        row = await self._store.create(
            self._store_name,
            self._codec.to_record(document, instance_id=self._instance_id),
        )
        return self._codec.from_record(row)

    async def merge(self, updates: dict[str, Any]) -> SectionDocument:
        existing = await self.get()
        body = dict(existing.body) if existing else {}
        body.update(updates)
        document = SectionDocument(
            body=body,
            source=(existing.source if existing else "local"),
            revision=(int(existing.revision) + 1 if existing else 1),
        )
        return await self.upsert(document)

    async def mutate(self, mutate_fn) -> SectionDocument:
        existing = await self.get()
        body = dict(existing.body) if existing else {}
        mutate_fn(body)
        document = SectionDocument(
            body=body,
            source=(existing.source if existing else "local"),
            revision=(int(existing.revision) + 1 if existing else 1),
        )
        return await self.upsert(document)

    async def upsert(self, document: SectionDocument) -> SectionDocument:
        identity = self._identity()
        existing_row = await self._load_row()
        if existing_row is not None:
            # YAML 单文档用 {} 匹配；DB 单例按自增 id
            key = identity
            if not key:
                key = {}
                if "id" in existing_row:
                    key = {"id": existing_row["id"]}
            row = await self._store.update(
                self._store_name,
                key,
                self._codec.to_updates(document),
            )
            if row is not None:
                return self._codec.from_record(row)
        row = await self._store.create(
            self._store_name,
            self._codec.to_record(document, instance_id=self._instance_id),
        )
        return self._codec.from_record(row)

    async def delete(self) -> bool:
        identity = self._identity()
        if identity:
            return await self._store.delete(self._store_name, identity)
        row = await self._load_row()
        if row is None:
            return False
        key: dict[str, Any] = {}
        if "id" in row:
            key = {"id": row["id"]}
        return await self._store.delete(self._store_name, key)


__all__ = [
    "DbBodySectionCodec",
    "DbFlatSectionCodec",
    "SectionDocument",
    "SectionDocumentCodec",
    "SectionDocumentRepository",
    "YamlSectionCodec",
]
