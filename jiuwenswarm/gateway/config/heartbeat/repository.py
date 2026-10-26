# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""heartbeat_config 领域：整段 ``/heartbeat``。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.section import (
    DbBodySectionCodec,
    SectionDocument,
    SectionDocumentRepository,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

HEARTBEAT_CONFIG_STORE_NAME = "heartbeat_config"

_HEARTBEAT_UPDATE_KEYS = ("every", "target", "active_hours")


class HeartbeatConfigRepository:
    """``heartbeat_config`` 读写；不判断 edition。"""

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
            HEARTBEAT_CONFIG_STORE_NAME,
            instance_id=instance_id,
        )

    async def get(self) -> SectionDocument | None:
        return await self._inner.get()

    async def get_body(self) -> dict[str, Any]:
        return await self._inner.get_body()

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        return await self._inner.replace(body)

    async def merge(self, updates: dict[str, Any]) -> SectionDocument:
        return await self._inner.merge(updates)

    async def mutate(self, mutate_fn) -> SectionDocument:
        return await self._inner.mutate(mutate_fn)

    async def delete(self) -> bool:
        return await self._inner.delete()

    async def merge_heartbeat_fields(self, payload: dict[str, Any]) -> SectionDocument:
        """对齐 ``update_heartbeat_in_config``：只合并 every / target / active_hours。"""
        updates = {
            key: payload[key]
            for key in _HEARTBEAT_UPDATE_KEYS
            if key in payload
        }
        return await self.merge(updates)


__all__ = [
    "HEARTBEAT_CONFIG_STORE_NAME",
    "HeartbeatConfigRepository",
]
