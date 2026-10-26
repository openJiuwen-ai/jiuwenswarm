# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""preferred_language_config 领域：顶层标量 ``preferred_language``。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.section import (
    DbBodySectionCodec,
    SectionDocument,
    SectionDocumentRepository,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

PREFERRED_LANGUAGE_CONFIG_STORE_NAME = "preferred_language_config"
_LANGUAGE_KEY = "preferred_language"
_VALID_LANGUAGES = frozenset({"zh", "en"})


def normalize_preferred_language(lang: Any) -> str:
    normalized = str(lang or "zh").strip().lower()
    if normalized not in _VALID_LANGUAGES:
        return "zh"
    return normalized


class PreferredLanguageConfigRepository:
    """``preferred_language_config`` 读写；不判断 edition。

    personal YAML 片段是标量；store record 形状为 ``{"preferred_language": "zh"}``。
    """

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
            PREFERRED_LANGUAGE_CONFIG_STORE_NAME,
            instance_id=instance_id,
        )

    async def get(self) -> SectionDocument | None:
        return await self._inner.get()

    async def get_body(self) -> dict[str, Any]:
        return await self._inner.get_body()

    async def get_language(self) -> str:
        body = await self.get_body()
        return normalize_preferred_language(body.get(_LANGUAGE_KEY))

    async def set_language(self, lang: str) -> SectionDocument:
        value = normalize_preferred_language(lang)
        return await self._inner.replace({_LANGUAGE_KEY: value})

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        if not isinstance(body, dict):
            raise ValueError("preferred_language body must be an object")
        value = normalize_preferred_language(body.get(_LANGUAGE_KEY, body.get("value")))
        return await self._inner.replace({_LANGUAGE_KEY: value})

    async def delete(self) -> bool:
        return await self._inner.delete()


__all__ = [
    "PREFERRED_LANGUAGE_CONFIG_STORE_NAME",
    "PreferredLanguageConfigRepository",
    "normalize_preferred_language",
]
