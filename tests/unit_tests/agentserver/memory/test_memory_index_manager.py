# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the async (aiosqlite) memory index manager.

These guard the refactor that moved the index database onto ``aiosqlite`` and
offloaded file reads, so that async callers no longer block the event loop and
migrations to ``status`` / ``_write_meta`` stay awaitable.
"""

import asyncio
import inspect

import pytest

from jiuwenswarm.agents.harness.common.memory.config import MemorySettings
from jiuwenswarm.agents.harness.common.memory.manager import MemoryIndexManager


def _mock_settings() -> MemorySettings:
    """Settings that use the in-process mock embedder and no watchdog thread."""
    settings = MemorySettings()
    settings.provider = "mock"
    settings.sources = ["memory"]
    settings.sync["watch"] = False
    return settings


def _write_memory(workspace, body: str) -> None:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "MEMORY.md").write_text(body, encoding="utf-8")


def test_status_and_meta_are_coroutine_functions():
    """The DB-touching methods must stay async after the aiosqlite migration."""
    assert inspect.iscoroutinefunction(MemoryIndexManager.status)
    assert inspect.iscoroutinefunction(MemoryIndexManager._write_meta)
    assert inspect.iscoroutinefunction(MemoryIndexManager._open_database)


@pytest.mark.asyncio
async def test_index_search_status_roundtrip(tmp_path):
    _write_memory(tmp_path, "# Memory\n\nalpha beta gamma\nsecond line delta\n")

    manager = await MemoryIndexManager.get("default", str(tmp_path), _mock_settings())
    assert manager is not None
    try:
        status = await manager.status()
        assert status["available"] is True
        assert status["files"] == 1
        assert status["chunks"] >= 1

        results = await manager.search("alpha beta gamma")
        assert results

        content = await manager.read_file("MEMORY.md")
        assert content["totalLines"] >= 2
        assert "alpha beta gamma" in content["text"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_concurrent_sync_and_search_are_serialized(tmp_path):
    body = "# Memory\n\n" + "\n".join(
        f"section {i} alpha beta gamma delta" for i in range(500)
    )
    _write_memory(tmp_path, body)

    manager = await MemoryIndexManager.get("default", str(tmp_path), _mock_settings())
    assert manager is not None
    try:
        # Concurrent full reindexes and searches exercise the write lock and the
        # single aiosqlite connection; none of them may raise.
        await asyncio.gather(
            manager.sync(reason="a", force=True),
            manager.sync(reason="b", force=True),
            manager.search("alpha"),
            manager.search("beta"),
        )

        status = await manager.status()
        assert status["files"] == 1
        assert status["chunks"] >= 1
    finally:
        await manager.close()
