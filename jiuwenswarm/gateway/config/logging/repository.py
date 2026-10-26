# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""logging_config 领域：整段 ``/logging``。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.section import (
    DbFlatSectionCodec,
    SectionDocument,
    SectionDocumentRepository,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

LOGGING_CONFIG_STORE_NAME = "logging_config"

LOGGING_LEVEL_FIELDS = (
    "level",
    "console_level",
    "gateway",
    "channel",
    "agent_server",
    "full",
)


class LoggingConfigRepository:
    """``logging_config`` 读写；不判断 edition。"""

    def __init__(
        self,
        store: PersistentStore,
        codec: YamlSectionCodec | DbFlatSectionCodec,
        *,
        instance_id: str = "",
    ) -> None:
        self._inner = SectionDocumentRepository(
            store,
            codec,
            LOGGING_CONFIG_STORE_NAME,
            instance_id=instance_id,
        )

    async def get(self) -> SectionDocument | None:
        return await self._inner.get()

    async def get_body(self) -> dict[str, Any]:
        return await self._inner.get_body()

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        return await self._inner.replace(body)

    async def merge_levels(self, updates: dict[str, Any]) -> SectionDocument:
        cleaned = {
            key: value
            for key, value in updates.items()
            if key in LOGGING_LEVEL_FIELDS and value is not None
        }
        return await self._inner.merge(cleaned)

    async def delete(self) -> bool:
        return await self._inner.delete()


__all__ = [
    "LOGGING_CONFIG_STORE_NAME",
    "LOGGING_LEVEL_FIELDS",
    "LoggingConfigRepository",
]
