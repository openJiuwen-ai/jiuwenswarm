# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""memory_config 领域：整段 ``/memory``（不含 ``modes.claw.*.memory``）。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.section import (
    DbBodySectionCodec,
    SectionDocument,
    SectionDocumentRepository,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

MEMORY_CONFIG_STORE_NAME = "memory_config"


class MemoryConfigRepository:
    """``memory_config`` 读写；不判断 edition。"""

    def __init__(
        self,
        store: PersistentStore,
        codec: YamlSectionCodec | DbBodySectionCodec,
        *,
        instance_id: str = "",
    ) -> None:
        self._inner = SectionDocumentRepository(
            store,
            codec,
            MEMORY_CONFIG_STORE_NAME,
            instance_id=instance_id,
        )

    async def get(self) -> SectionDocument | None:
        return await self._inner.get()

    async def get_body(self) -> dict[str, Any]:
        return await self._inner.get_body()

    async def upsert_body(
        self,
        body: dict[str, Any],
        *,
        source: str = "manager",
    ) -> SectionDocument:
        """企业配置 upsert：整段替换 body 并递增 revision。"""
        existing = await self.get()
        document = SectionDocument(
            body=dict(body),
            source=source,
            revision=(int(existing.revision) + 1 if existing else 1),
        )
        return await self._inner.upsert(document)

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        return await self._inner.replace(body)

    async def merge(self, updates: dict[str, Any]) -> SectionDocument:
        return await self._inner.merge(updates)

    async def mutate(self, mutate_fn) -> SectionDocument:
        return await self._inner.mutate(mutate_fn)

    async def delete(self) -> bool:
        return await self._inner.delete()

    async def set_forbidden_enabled(self, value: bool) -> SectionDocument:
        def _mutate(body: dict[str, Any]) -> None:
            section = body.get("forbidden_memory_definition")
            if not isinstance(section, dict):
                section = {}
                body["forbidden_memory_definition"] = section
            section["enabled"] = bool(value)

        return await self.mutate(_mutate)

    async def merge_forbidden(self, updates: dict[str, Any]) -> SectionDocument:
        def _mutate(body: dict[str, Any]) -> None:
            section = body.get("forbidden_memory_definition")
            if not isinstance(section, dict):
                section = {}
                body["forbidden_memory_definition"] = section
            section.update(updates)

        return await self.mutate(_mutate)

    async def merge_forbidden_description(
        self, description: dict[str, str]
    ) -> SectionDocument:
        def _mutate(body: dict[str, Any]) -> None:
            section = body.get("forbidden_memory_definition")
            if not isinstance(section, dict):
                section = {}
                body["forbidden_memory_definition"] = section
            current = section.get("description")
            if isinstance(current, dict) and description:
                section["description"] = {**current, **description}
            else:
                section["description"] = dict(description)

        return await self.mutate(_mutate)


__all__ = [
    "MEMORY_CONFIG_STORE_NAME",
    "MemoryConfigRepository",
]
