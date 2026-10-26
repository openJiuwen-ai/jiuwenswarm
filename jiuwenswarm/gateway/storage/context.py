# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""存储上下文：注入 Persistent + Ephemeral，不解析产品形态。"""

from __future__ import annotations

from collections.abc import Callable

from jiuwenswarm.gateway.storage.protocols.ephemeral import EphemeralStore
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore


class StorageContext:
    """进程内存储 facade（Persistent + Ephemeral）。"""

    def __init__(
        self,
        persistent: PersistentStore,
        *,
        ephemeral_factory: Callable[[str], EphemeralStore],
    ) -> None:
        self._persistent = persistent
        self._ephemeral_factory = ephemeral_factory
        self._ephemeral_stores: dict[str, EphemeralStore] = {}
        self._persistent_ready = False

    async def persistent(self) -> PersistentStore:
        if not self._persistent_ready:
            await self._persistent.ensure_ready()
            self._persistent_ready = True
        return self._persistent

    def ephemeral(self, namespace: str) -> EphemeralStore:
        ns = str(namespace or "default").strip() or "default"
        store = self._ephemeral_stores.get(ns)
        if store is None:
            store = self._ephemeral_factory(ns)
            self._ephemeral_stores[ns] = store
        return store

    async def shutdown(self) -> None:
        await self._persistent.close()
        self._persistent_ready = False


__all__ = ["StorageContext"]
