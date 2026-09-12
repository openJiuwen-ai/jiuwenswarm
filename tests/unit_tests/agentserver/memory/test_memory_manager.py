# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.memory.manager import (
    EMBEDDING_CACHE_TABLE,
    MemoryIndexManager,
)


class MemoryIndexManagerForTest(MemoryIndexManager):
    async def get_embedding(self, text: str) -> list[float] | None:
        return await self._get_embedding(text)


def create_settings() -> SimpleNamespace:
    return SimpleNamespace(
        store={
            "fts": {"enabled": True},
            "vector": {"enabled": True},
        },
        cache={"enabled": True},
        sources=(),
    )


@pytest.mark.asyncio
async def test_sync_serializes_concurrent_calls() -> None:
    manager = MemoryIndexManager("test-agent", ".", create_settings())

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    active_calls = 0
    max_active_calls = 0

    async def should_full_reindex() -> bool:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        if active_calls == 1:
            first_entered.set()
            await release_first.wait()
        active_calls -= 1
        return False

    with patch.object(
        manager,
        "_should_full_reindex",
        side_effect=should_full_reindex,
    ):
        first = asyncio.create_task(manager.sync(reason="first"))
        await first_entered.wait()
        second = asyncio.create_task(manager.sync(reason="second"))
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, second)

    assert max_active_calls == 1


@pytest.mark.asyncio
async def test_close_waits_for_active_sync() -> None:
    manager = MemoryIndexManager("test-agent", ".", create_settings())
    manager.db = sqlite3.connect(":memory:")

    sync_entered = asyncio.Event()
    release_sync = asyncio.Event()

    async def should_full_reindex() -> bool:
        sync_entered.set()
        await release_sync.wait()
        manager.db.execute("SELECT 1")
        return False

    with patch.object(
        manager,
        "_should_full_reindex",
        side_effect=should_full_reindex,
    ):
        sync_task = asyncio.create_task(manager.sync(reason="test"))
        await sync_entered.wait()
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)

        assert not close_task.done()
        assert not manager.closed

        release_sync.set()
        await asyncio.gather(sync_task, close_task)

    assert manager.closed
    with pytest.raises(sqlite3.ProgrammingError):
        manager.db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_get_embedding_commits_cache_write() -> None:
    class Provider:
        id = "test-provider"
        model = "test-model"

        async def embed_query(self, text: str) -> list[float]:
            return [1.0, 2.0]

    manager = MemoryIndexManagerForTest("test-agent", ".", create_settings())
    manager.provider = Provider()
    manager.provider_key = "test-key"
    manager.cache_enabled = True
    manager.db = sqlite3.connect(":memory:")
    manager.db.execute(
        f"""
        CREATE TABLE {EMBEDDING_CACHE_TABLE} (
            provider TEXT,
            model TEXT,
            provider_key TEXT,
            hash TEXT,
            embedding BLOB,
            dims INTEGER,
            updated_at INTEGER
        )
        """
    )
    manager.db.commit()

    try:
        embedding = await manager.get_embedding("test input")

        assert embedding == [1.0, 2.0]
        assert not manager.db.in_transaction
        cached_rows = manager.db.execute(
            f"SELECT COUNT(*) FROM {EMBEDDING_CACHE_TABLE}"
        ).fetchone()[0]
        assert cached_rows == 1
    finally:
        manager.db.rollback()
        manager.db.close()
