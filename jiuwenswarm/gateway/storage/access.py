# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 进程内 ``PersistentStore`` 注入入口（config_poll / enterprise cron 等共用）。"""

from __future__ import annotations

from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

_store: PersistentStore | None = None


def set_persistent_store(store: PersistentStore | None) -> None:
    global _store
    _store = store


def get_persistent_store() -> PersistentStore | None:
    return _store


def clear_persistent_store() -> None:
    set_persistent_store(None)


async def require_persistent_store() -> PersistentStore:
    store = get_persistent_store()
    if store is None:
        raise RuntimeError(
            "PersistentStore is not wired; "
            "ensure setup_gateway_storage_repositories / "
            "wire_enterprise_persistent_repositories_async ran at Gateway startup"
        )
    await store.ensure_ready()
    return store


__all__ = [
    "clear_persistent_store",
    "get_persistent_store",
    "require_persistent_store",
    "set_persistent_store",
]
