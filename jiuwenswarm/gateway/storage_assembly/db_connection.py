# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway enterprise DB 连接。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from jiuwenswarm.gateway.storage.errors import StorageUnavailableError


def resolve_gateway_replicas() -> int:
    """解析 ``GATEWAY_REPLICAS``；缺省为 1。"""
    raw = os.getenv("GATEWAY_REPLICAS")
    if raw is None or not str(raw).strip():
        return 1
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise StorageUnavailableError(f"invalid GATEWAY_REPLICAS={raw!r}") from exc
    if value < 1:
        raise StorageUnavailableError(f"invalid GATEWAY_REPLICAS={raw!r}")
    return value


def resolve_gateway_db_type() -> str:
    """解析 ``GATEWAY_DB_TYPE``（可回退 ``DB_TYPE``）；缺省 ``sqlite``。"""
    raw = os.getenv("GATEWAY_DB_TYPE") or os.getenv("DB_TYPE") or "sqlite"
    return str(raw).strip().lower() or "sqlite"


def assert_replicas_db_compat() -> None:
    """``GATEWAY_REPLICAS > 1`` 时禁止 sqlite。"""
    from openjiuwen_runtime.foundation.db.utils import is_sqlite

    replicas = resolve_gateway_replicas()
    db_type = resolve_gateway_db_type()
    if replicas > 1 and is_sqlite(db_type):
        raise StorageUnavailableError(
            f"GATEWAY_REPLICAS={replicas} forbids sqlite; "
            "set GATEWAY_DB_TYPE to mysql or postgresql"
        )


class GatewayDbConnection:
    """绑定 ``Database`` 进程内单例。建表只在首次 ``ensure_ready`` 执行。"""

    def __init__(self) -> None:
        self._db_obj: Any | None = None
        self._handler: Any | None = None
        self._lock = asyncio.Lock()

    def _bind_database(self) -> Any:
        from jiuwenswarm.infrastructure.db.database import Database

        db = Database.current()
        self._db_obj = db
        return db

    async def ensure_ready(self) -> Any:
        if self._handler is not None:
            return self._handler
        async with self._lock:
            if self._handler is not None:
                return self._handler
            assert_replicas_db_compat()
            db = self._db_obj or self._bind_database()
            # ``Database.ensure_ready`` 已调用 ``init_all_tables``（幂等）。
            handler = await db.ensure_ready(log_prefix="gateway_storage")
            self._handler = handler
            return handler

    async def close(self) -> None:
        async with self._lock:
            db = self._db_obj
            self._db_obj = None
            self._handler = None
            if db is None:
                return
            await db.close()


__all__ = [
    "GatewayDbConnection",
    "assert_replicas_db_compat",
    "resolve_gateway_db_type",
    "resolve_gateway_replicas",
]
