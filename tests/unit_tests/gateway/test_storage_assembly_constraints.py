# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配层硬约束：Redis 必填、多副本禁 sqlite、建表只一次。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jiuwenswarm.gateway.storage.backends.redis_ephemeral import RedisEphemeralBackend
from jiuwenswarm.gateway.storage.errors import StorageUnavailableError
from jiuwenswarm.gateway.storage_assembly.db_connection import (
    GatewayDbConnection,
    assert_replicas_db_compat,
)
from jiuwenswarm.gateway.storage_assembly.setup import create_gateway_storage_context

_ENTERPRISE_CFG = {"gateway": {"edition": "enterprise"}}


def test_replicas_sqlite_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "2")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    with pytest.raises(StorageUnavailableError, match="forbids sqlite"):
        assert_replicas_db_compat()


def test_replicas_mysql_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "2")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "mysql")
    assert_replicas_db_compat()


def test_single_replica_sqlite_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "1")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    assert_replicas_db_compat()


def test_enterprise_context_requires_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_REPLICAS", "1")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.storage_assembly.setup._redis_client",
        lambda: None,
    )
    with pytest.raises(StorageUnavailableError, match="requires Redis"):
        create_gateway_storage_context(_ENTERPRISE_CFG)


def test_enterprise_context_replicas_forbid_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_REPLICAS", "2")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.storage_assembly.setup._redis_client",
        lambda: object(),
    )
    with pytest.raises(StorageUnavailableError, match="forbids sqlite"):
        create_gateway_storage_context(_ENTERPRISE_CFG)


def test_enterprise_context_uses_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_REPLICAS", "2")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "postgresql")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.storage_assembly.setup._redis_client",
        lambda: object(),
    )
    ctx = create_gateway_storage_context(_ENTERPRISE_CFG)
    store = ctx.ephemeral("web_ws")
    assert isinstance(store, RedisEphemeralBackend)
    assert store.available


@pytest.mark.asyncio
async def test_ensure_ready_delegates_to_gateway_db_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "1")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    calls: list[str] = []
    handler = object()

    class _FakeDb:
        async def ensure_ready(self, log_prefix: str = "") -> object:
            calls.append(log_prefix)
            return handler

        async def close(self) -> None:
            return None

    conn = GatewayDbConnection()
    monkeypatch.setattr(conn, "_bind_database", lambda: _FakeDb())

    first = await conn.ensure_ready()
    second = await conn.ensure_ready()
    assert first is handler
    assert second is handler
    assert calls == ["gateway_storage"]


@pytest.mark.asyncio
async def test_ensure_ready_replicas_forbid_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "2")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    conn = GatewayDbConnection()
    bind = MagicMock()
    monkeypatch.setattr(conn, "_bind_database", bind)
    with pytest.raises(StorageUnavailableError, match="forbids sqlite"):
        await conn.ensure_ready()
    bind.assert_not_called()


@pytest.mark.asyncio
async def test_close_allows_init_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REPLICAS", "1")
    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    calls: list[str] = []
    handler = object()

    class _FakeDb:
        async def ensure_ready(self, log_prefix: str = "") -> object:
            calls.append(log_prefix)
            return handler

        async def close(self) -> None:
            return None

    conn = GatewayDbConnection()
    monkeypatch.setattr(conn, "_bind_database", lambda: _FakeDb())
    await conn.ensure_ready()
    await conn.close()
    await conn.ensure_ready()
    assert calls == ["gateway_storage", "gateway_storage"]
