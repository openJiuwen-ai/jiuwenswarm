# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""持久化 DB 连接协议。具体实现由装配层注入。"""

from __future__ import annotations

from typing import Any, Protocol


class PersistentDbConnection(Protocol):
    """DbPersistentBackend 所需的连接生命周期。"""

    async def ensure_ready(self) -> Any:
        """返回可执行 CRUD 的 handler。"""

    async def close(self) -> None:
        ...


__all__ = ["PersistentDbConnection"]
