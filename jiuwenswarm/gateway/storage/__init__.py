# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""存储屏蔽层（Persistent + Ephemeral）。"""

from jiuwenswarm.gateway.storage.backends import (
    DbPersistentBackend,
    FilePersistentBackend,
    InMemoryPersistentBackend,
    MemoryEphemeralBackend,
    PersistentDbConnection,
    RedisEphemeralBackend,
)
from jiuwenswarm.gateway.storage.context import StorageContext
from jiuwenswarm.gateway.storage.errors import DuplicateRecordError, StorageUnavailableError
from jiuwenswarm.gateway.storage.protocols.ephemeral import EphemeralStore
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore
from jiuwenswarm.gateway.storage.registry import (
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
    "InMemoryPersistentBackend",
    "PersistentDbConnection",
    "MemoryEphemeralBackend",
    "RedisEphemeralBackend",
    "StorageUnavailableError",
    "DuplicateRecordError",
]
