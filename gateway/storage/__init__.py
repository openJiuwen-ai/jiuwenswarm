# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""存储屏蔽层（Persistent + Ephemeral）。"""

from __future__ import annotations

from typing import Any

from gateway.storage.backends import (
    MemoryEphemeralBackend,
    RedisEphemeralBackend,
)
from gateway.storage.context import StorageContext
from gateway.storage.errors import DuplicateRecordError, StorageUnavailableError
from gateway.storage.protocols.ephemeral import EphemeralStore
from gateway.storage.protocols.persistent import PersistentStore
from gateway.storage.registry import (
    DbLayout,
    FileLayout,
    StoreLayout,
    StoreRegistry,
)

__all__ = [
    "StorageContext",
    "PersistentStore",
    "EphemeralStore",
    "StoreRegistry",
    "StoreLayout",
    "FileLayout",
    "DbLayout",
    "FilePersistentBackend",
    "DbPersistentBackend",
    "PersistentDbConnection",
    "MemoryEphemeralBackend",
    "RedisEphemeralBackend",
    "StorageUnavailableError",
    "DuplicateRecordError",
]


def __getattr__(name: str) -> Any:
    if name == "FilePersistentBackend":
        from gateway.storage.backends import FilePersistentBackend

        return FilePersistentBackend
    if name in ("DbPersistentBackend", "PersistentDbConnection"):
        from gateway.storage.backends import (
            DbPersistentBackend,
            PersistentDbConnection,
        )

        return {
            "DbPersistentBackend": DbPersistentBackend,
            "PersistentDbConnection": PersistentDbConnection,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
